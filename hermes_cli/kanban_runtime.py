"""Bind dispatcher-managed Kanban workspaces to local worker execution.

The assignee profile supplies identity and credentials, while the dispatcher
materializes the workspace on its own host.  Its private environment marker
must survive profile config and dotenv loading so an SSH terminal setting
cannot send local workspace paths to another machine.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, MutableMapping, Optional


KANBAN_EXECUTION_BACKEND_ENV = "_HERMES_KANBAN_EXECUTION_BACKEND"
KANBAN_LOCAL_EXECUTION_BACKEND = "local"
# The launch env's TERMINAL_* values the pin removed or overwrote (JSON): the worker client hands
# them back to an owner it auto-starts, whose backend may be env-only (systemd / ``op run``).
KANBAN_PREPIN_TERMINAL_ENV = "HERMES_KANBAN_PREPIN_TERMINAL"

_REMOTE_TERMINAL_PREFIXES = (
    "TERMINAL_CONTAINER_",
    "TERMINAL_DAYTONA_",
    "TERMINAL_DOCKER_",
    "TERMINAL_MODAL_",
    "TERMINAL_SINGULARITY_",
    "TERMINAL_SSH_",
)
_REMOTE_TERMINAL_KEYS = {"TERMINAL_PERSISTENT_SHELL"}


class KanbanExecutionContractError(RuntimeError):
    """Raised before work starts when workspace and backend cannot be paired."""


@dataclass(frozen=True)
class KanbanExecutionContract:
    backend: str
    workspace: str


def _validated_local_workspace(raw_workspace: object) -> str:
    workspace = str(raw_workspace or "").strip()
    if not workspace:
        raise KanbanExecutionContractError(
            "kanban workspace/backend mismatch: local execution requires a workspace"
        )
    path = Path(workspace)
    if not path.is_absolute():
        raise KanbanExecutionContractError(
            "kanban workspace/backend mismatch: dispatcher-managed workspace "
            f"must be absolute, got {workspace!r}"
        )
    if not path.is_dir():
        raise KanbanExecutionContractError(
            "kanban workspace/backend mismatch: dispatcher-local workspace "
            f"does not exist or is not a directory: {workspace}"
        )
    return workspace


def _pin_local_environment(
    env: MutableMapping[str, str],
    workspace: str,
    *,
    terminal_config: Optional[dict] = None,
) -> None:
    for key in tuple(env):
        if key in _REMOTE_TERMINAL_KEYS or key.startswith(_REMOTE_TERMINAL_PREFIXES):
            env.pop(key, None)
    env["TERMINAL_ENV"] = KANBAN_LOCAL_EXECUTION_BACKEND
    env["TERMINAL_CWD"] = workspace

    if terminal_config is not None:
        terminal_config["backend"] = KANBAN_LOCAL_EXECUTION_BACKEND
        terminal_config["env_type"] = KANBAN_LOCAL_EXECUTION_BACKEND
        terminal_config["cwd"] = workspace


def bind_local_worker_execution(
    env: MutableMapping[str, str], workspace: object
) -> KanbanExecutionContract:
    """Write the dispatcher-issued execution contract before worker spawn."""
    if not str(env.get("HERMES_KANBAN_TASK") or "").strip():
        raise KanbanExecutionContractError(
            "kanban workspace/backend mismatch: worker task identity is missing"
        )
    normalized_workspace = _validated_local_workspace(workspace)
    prepin = {
        key: value for key, value in env.items()
        if key in ("TERMINAL_ENV", "TERMINAL_CWD") or key in _REMOTE_TERMINAL_KEYS
        or key.startswith(_REMOTE_TERMINAL_PREFIXES)
    }
    if prepin:
        env[KANBAN_PREPIN_TERMINAL_ENV] = json.dumps(prepin, sort_keys=True)
    else:
        env.pop(KANBAN_PREPIN_TERMINAL_ENV, None)
    env["HERMES_KANBAN_WORKSPACE"] = normalized_workspace
    env[KANBAN_EXECUTION_BACKEND_ENV] = KANBAN_LOCAL_EXECUTION_BACKEND
    _pin_local_environment(env, normalized_workspace)
    return KanbanExecutionContract(
        backend=KANBAN_LOCAL_EXECUTION_BACKEND, workspace=normalized_workspace
    )


def pin_local_terminal_scope(
    terminal: MutableMapping[str, object], workspace: object
) -> KanbanExecutionContract:
    """Pin an owner session's frozen terminal policy to local execution in *workspace*."""
    normalized_workspace = _validated_local_workspace(workspace)
    _pin_local_environment(terminal, normalized_workspace)
    return KanbanExecutionContract(
        backend=KANBAN_LOCAL_EXECUTION_BACKEND, workspace=normalized_workspace
    )


def verify_local_terminal_scope(
    terminal: Mapping[str, object], workspace: object
) -> KanbanExecutionContract:
    """Refuse an owner-run Kanban turn whose terminal policy is not pinned local."""
    normalized_workspace = _validated_local_workspace(workspace)
    backend = str(terminal.get("TERMINAL_ENV") or "").strip().lower()
    if backend != KANBAN_LOCAL_EXECUTION_BACKEND:
        raise KanbanExecutionContractError(
            "kanban workspace/backend mismatch: dispatcher-managed workspaces "
            f"require local execution, got {backend or 'unset'!r}"
        )
    if terminal.get("TERMINAL_CWD") != normalized_workspace:
        raise KanbanExecutionContractError(
            "kanban workspace/backend mismatch: terminal cwd and trusted workspace "
            f"differ ({terminal.get('TERMINAL_CWD')!r} != {normalized_workspace})"
        )
    remote = sorted(
        key for key in terminal
        if key in _REMOTE_TERMINAL_KEYS or key.startswith(_REMOTE_TERMINAL_PREFIXES)
    )
    if remote:
        raise KanbanExecutionContractError(
            "kanban workspace/backend mismatch: local terminal policy still carries "
            f"remote settings {remote}"
        )
    return KanbanExecutionContract(
        backend=KANBAN_LOCAL_EXECUTION_BACKEND, workspace=normalized_workspace
    )


def _remote_terminal_config_keys(section: Mapping[str, object]) -> list[str]:
    from hermes_cli.config import TERMINAL_CONFIG_ENV_MAP

    return sorted(
        key for key in section
        if (env := TERMINAL_CONFIG_ENV_MAP.get(key, f"TERMINAL_{str(key).upper()}")) in _REMOTE_TERMINAL_KEYS
        or env.startswith(_REMOTE_TERMINAL_PREFIXES)
    )


def pin_local_terminal_config(
    section: MutableMapping[str, object], workspace: object
) -> KanbanExecutionContract:
    """Pin a session's frozen ``terminal:`` config section to local execution in *workspace*.

    The managed worker binds the whole frozen config, so config readers (and its env bridge)
    would otherwise still see the profile's ssh backend and hosts.
    """
    normalized_workspace = _validated_local_workspace(workspace)
    for key in _remote_terminal_config_keys(section):
        section.pop(key, None)
    _pin_local_environment({}, normalized_workspace, terminal_config=section)  # type: ignore[arg-type]
    return KanbanExecutionContract(
        backend=KANBAN_LOCAL_EXECUTION_BACKEND, workspace=normalized_workspace
    )


def verify_local_terminal_config(
    section: object, workspace: object
) -> KanbanExecutionContract:
    """Refuse an owner-run Kanban turn whose frozen ``terminal:`` config is not pinned local."""
    normalized_workspace = _validated_local_workspace(workspace)
    if not isinstance(section, Mapping):
        raise KanbanExecutionContractError(
            "kanban workspace/backend mismatch: frozen config has no local terminal section"
        )
    backend = str(section.get("backend") or "").strip().lower()
    if backend != KANBAN_LOCAL_EXECUTION_BACKEND or section.get("cwd") != normalized_workspace:
        raise KanbanExecutionContractError(
            "kanban workspace/backend mismatch: frozen terminal config is "
            f"{backend or 'unset'!r} in {section.get('cwd')!r}, not local in {normalized_workspace}"
        )
    remote = _remote_terminal_config_keys(section)
    if remote:
        raise KanbanExecutionContractError(
            "kanban workspace/backend mismatch: frozen terminal config still carries "
            f"remote settings {remote}"
        )
    return KanbanExecutionContract(
        backend=KANBAN_LOCAL_EXECUTION_BACKEND, workspace=normalized_workspace
    )


def apply_worker_execution_contract(
    env: Optional[MutableMapping[str, str]] = None,
    *,
    terminal_config: Optional[dict] = None,
) -> Optional[KanbanExecutionContract]:
    """Reapply the contract after loading profile config or dotenv overlays."""
    target = os.environ if env is None else env
    backend = str(target.get(KANBAN_EXECUTION_BACKEND_ENV) or "").strip().lower()
    if not backend:
        return None
    if backend != KANBAN_LOCAL_EXECUTION_BACKEND:
        raise KanbanExecutionContractError(
            "kanban workspace/backend mismatch: dispatcher-managed workspaces "
            f"require local execution, got {backend!r}"
        )
    if not str(target.get("HERMES_KANBAN_TASK") or "").strip():
        raise KanbanExecutionContractError(
            "kanban workspace/backend mismatch: execution contract has no task identity"
        )

    workspace = _validated_local_workspace(target.get("HERMES_KANBAN_WORKSPACE"))
    try:
        same_cwd = os.path.samefile(os.getcwd(), workspace)
    except OSError as exc:
        raise KanbanExecutionContractError(
            "kanban workspace/backend mismatch: could not verify worker process cwd "
            f"against {workspace}: {exc}"
        ) from exc
    if not same_cwd:
        raise KanbanExecutionContractError(
            "kanban workspace/backend mismatch: worker process cwd and trusted "
            f"workspace differ ({os.getcwd()} != {workspace})"
        )

    _pin_local_environment(target, workspace, terminal_config=terminal_config)
    return KanbanExecutionContract(backend=backend, workspace=workspace)
