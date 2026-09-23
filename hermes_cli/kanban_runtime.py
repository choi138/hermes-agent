"""Bind dispatcher-managed Kanban workspaces to local worker execution.

The assignee profile supplies identity and credentials, while the dispatcher
materializes the workspace on its own host.  Its private environment marker
must survive profile config and dotenv loading so an SSH terminal setting
cannot send local workspace paths to another machine.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import MutableMapping, Optional


KANBAN_EXECUTION_BACKEND_ENV = "_HERMES_KANBAN_EXECUTION_BACKEND"
KANBAN_LOCAL_EXECUTION_BACKEND = "local"

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
    env["HERMES_KANBAN_WORKSPACE"] = normalized_workspace
    env[KANBAN_EXECUTION_BACKEND_ENV] = KANBAN_LOCAL_EXECUTION_BACKEND
    _pin_local_environment(env, normalized_workspace)
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
