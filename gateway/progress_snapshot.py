"""Semantic, secret-safe progress snapshots for chat gateways.

The tracker deliberately renders only a small fixed vocabulary. Tool names,
arguments, previews, and results are used solely to classify the current phase
and are never copied into user-facing text.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Any


@dataclass(frozen=True)
class ProgressSnapshot:
    """A compact description of what is happening and what is known."""

    stage: str
    confirmed: str
    next_action: str

    def render(self) -> str:
        return (
            f"**Current stage:** {self.stage}\n"
            f"**Confirmed:** {self.confirmed}\n"
            f"**Next:** {self.next_action}"
        )


_STARTED_COPY = {
    "inspect": (
        "Inspecting the current state",
        "Use the inspection results to identify the next safe action",
    ),
    "diagnose": (
        "Diagnosing the issue",
        "Turn the evidence into a specific, testable cause",
    ),
    "modify": (
        "Applying the change",
        "Verify the changed behavior before deployment",
    ),
    "verify": (
        "Verifying the changes",
        "Use the verification result to decide whether the change is ready",
    ),
    "deploy": (
        "Deploying the verified change",
        "Confirm the service restarted and is serving the new version",
    ),
    "observe": (
        "Observing runtime health",
        "Confirm the target behavior and check for new errors",
    ),
}

_SUCCESS_COPY = {
    "inspect": (
        "Inspection completed successfully",
        "Diagnose the issue using the confirmed evidence",
    ),
    "diagnose": (
        "Diagnostic step completed successfully",
        "Apply the smallest change that addresses the confirmed cause",
    ),
    "modify": (
        "Change applied successfully",
        "Run focused verification before deployment",
    ),
    "verify": (
        "Verification completed successfully",
        "Deploy the verified change or continue broader checks",
    ),
    "deploy": (
        "Deployment step completed successfully",
        "Observe service health and the target behavior",
    ),
    "observe": (
        "Runtime observation completed successfully",
        "Compare the observed behavior with the acceptance criteria",
    ),
}

_FAILURE_STAGE = {
    "inspect": "Inspection needs another approach",
    "diagnose": "Diagnosis needs another approach",
    "modify": "The change could not be applied",
    "verify": "Verification found a problem",
    "deploy": "Deployment needs attention",
    "observe": "Runtime observation found a problem",
}

_DEPLOY_MARKERS = (
    " deploy",
    "deployment",
    "systemctl restart",
    "service restart",
    "gateway restart",
    "docker compose restart",
    "docker compose up",
    "kubectl apply",
    "kubectl rollout",
    "helm upgrade",
    "git push",
    "git pull",
    " rsync",
    " scp",
)

_REMOTE_EXECUTION_MARKERS = (" ssh",)

_VERIFY_MARKERS = (
    "run_tests.sh",
    "pytest",
    "unittest",
    "npm test",
    "pnpm test",
    "yarn test",
    "go test",
    "cargo test",
    "gradle test",
    " ruff",
    "flake8",
    "mypy",
    "pyright",
    " lint",
    "typecheck",
    "diff --check",
)

_OBSERVE_MARKERS = (
    "journalctl",
    "logs",
    "status",
    "health",
    "monitor",
    "systemctl status",
    "service status",
    "healthcheck",
    "health check",
    "healthz",
    "metrics",
    " log tail",
    "tail -f",
    "docker logs",
    "kubectl logs",
    "vmstat",
    "free -",
    " top",
)

_MODIFY_MARKERS = (
    "apply_patch",
    "patch_file",
    "write_file",
    "edit_file",
    "create_file",
    "kanban_update",
    "kanban_create",
    "kanban_move",
    "sed -i",
    "install -m",
)

_DIAGNOSE_MARKERS = (
    "diagnose",
    "debug",
    "analyze",
    "analyse",
    "traceback",
    "stack trace",
    "sentry",
    "profile",
)

_INSPECT_NAME_MARKERS = (
    "show",
    "list",
    "read",
    "view",
    "search",
    "fetch",
    "get",
    "find",
    "grep",
    "browser",
    "kanban",
)


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    padded = f" {text}"
    return any(marker in padded for marker in markers)


def _classification_text(
    tool_name: str | None,
    preview: str | None,
    args: dict[str, Any] | None,
) -> str:
    fragments = [str(tool_name or ""), str(preview or "")]
    if isinstance(args, dict):
        for key in ("command", "cmd", "script", "action", "operation", "subcommand"):
            value = args.get(key)
            if isinstance(value, str):
                fragments.append(value)
    return " ".join(fragments).casefold()


def classify_progress_phase(
    tool_name: str | None,
    preview: str | None = None,
    args: dict[str, Any] | None = None,
) -> str:
    """Classify a tool call without exposing any of its raw details."""

    text = _classification_text(tool_name, preview, args)

    # Strong deployment actions take precedence over everything else. Generic
    # remote execution is classified later so an SSH test remains verification
    # and an SSH log read remains observation.
    if _contains_any(text, _DEPLOY_MARKERS):
        return "deploy"
    if _contains_any(text, _VERIFY_MARKERS):
        return "verify"
    if _contains_any(text, _MODIFY_MARKERS):
        return "modify"
    if _contains_any(text, _OBSERVE_MARKERS):
        return "observe"
    if _contains_any(text, _DIAGNOSE_MARKERS):
        return "diagnose"
    if _contains_any(text, _REMOTE_EXECUTION_MARKERS):
        return "deploy"
    if any(marker in text for marker in _INSPECT_NAME_MARKERS):
        return "inspect"
    return "inspect"


class SemanticProgressTracker:
    """Track semantic phases and confirmed-success evidence for one run."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._active_phases: dict[str, list[str]] = {}
        self._snapshot = ProgressSnapshot(
            stage="Preparing the request",
            confirmed="Request received",
            next_action="Inspect the relevant state and gather evidence",
        )

    @property
    def snapshot(self) -> ProgressSnapshot:
        with self._lock:
            return self._snapshot

    def tool_started(
        self,
        tool_name: str | None,
        preview: str | None = None,
        args: dict[str, Any] | None = None,
    ) -> ProgressSnapshot:
        phase = classify_progress_phase(tool_name, preview, args)
        key = str(tool_name or "")
        with self._lock:
            self._active_phases.setdefault(key, []).append(phase)
            stage, next_action = _STARTED_COPY[phase]
            self._snapshot = ProgressSnapshot(
                stage=stage,
                confirmed=self._snapshot.confirmed,
                next_action=next_action,
            )
            return self._snapshot

    def tool_completed(
        self,
        tool_name: str | None,
        *,
        is_error: bool,
        result: Any = None,
    ) -> ProgressSnapshot:
        # ``result`` is accepted so callers can forward the lifecycle payload,
        # but is intentionally never inspected or retained.
        del result
        key = str(tool_name or "")
        with self._lock:
            active = self._active_phases.get(key)
            phase = active.pop(0) if active else classify_progress_phase(tool_name)
            if active == []:
                self._active_phases.pop(key, None)

            if is_error:
                self._snapshot = ProgressSnapshot(
                    stage=_FAILURE_STAGE[phase],
                    confirmed=self._snapshot.confirmed,
                    next_action="Review the failure and choose a safe retry or alternative",
                )
            else:
                confirmed, next_action = _SUCCESS_COPY[phase]
                self._snapshot = ProgressSnapshot(
                    stage=_STARTED_COPY[phase][0],
                    confirmed=confirmed,
                    next_action=next_action,
                )
            return self._snapshot

    def cancelled(self) -> ProgressSnapshot:
        with self._lock:
            self._active_phases.clear()
            self._snapshot = ProgressSnapshot(
                stage="Request stopped",
                confirmed="No work was started",
                next_action="Send the request again when ready",
            )
            return self._snapshot
