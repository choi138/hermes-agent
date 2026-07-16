"""Semantic, secret-safe progress snapshots for chat gateways.

The tracker deliberately renders only a small fixed vocabulary. Tool names,
arguments, previews, and results are used solely to classify the current phase
and are never copied into user-facing text.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Any

from agent.i18n import get_language, t


@dataclass(frozen=True)
class ProgressSnapshot:
    """A compact description of what is happening and what is known."""

    stage: str
    confirmed: str
    next_action: str
    language: str | None = None

    def render(self) -> str:
        return t(
            "gateway.progress.template",
            lang=self.language,
            stage=self.stage,
            confirmed=self.confirmed,
            next_action=self.next_action,
        )


_PHASES = frozenset({"inspect", "diagnose", "modify", "verify", "deploy", "observe"})


def resolve_progress_language(config: dict[str, Any] | None = None) -> str:
    """Resolve progress copy from an already profile-scoped config.

    Gateway multiplexing temporarily scopes ``HERMES_HOME`` while loading a
    profile's config, so resolving the language later through a process-global
    config lookup can select the wrong bot's language.  Callers pass the config
    they already loaded; other surfaces retain the normal i18n resolution.
    """

    if isinstance(config, dict):
        display = config.get("display")
        if isinstance(display, dict):
            language = display.get("language")
            if isinstance(language, str) and language.strip():
                return language.strip()
    return get_language()


def _copy(key: str, language: str) -> str:
    return t(f"gateway.progress.{key}", lang=language)

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

    def __init__(self, *, language: str | None = None) -> None:
        self.language = language or get_language()
        self._lock = Lock()
        self._active_phases: dict[str, list[str]] = {}
        self._snapshot = ProgressSnapshot(
            stage=_copy("stage.preparing", self.language),
            confirmed=_copy("confirmed.request_received", self.language),
            next_action=_copy("next.initial", self.language),
            language=self.language,
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
        if phase not in _PHASES:  # defensive if the classifier grows later
            phase = "inspect"
        key = str(tool_name or "")
        with self._lock:
            self._active_phases.setdefault(key, []).append(phase)
            self._snapshot = ProgressSnapshot(
                stage=_copy(f"stage.{phase}", self.language),
                confirmed=self._snapshot.confirmed,
                next_action=_copy("next.active", self.language),
                language=self.language,
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
                    stage=_copy("stage.failure", self.language),
                    confirmed=self._snapshot.confirmed,
                    next_action=_copy("next.failure", self.language),
                    language=self.language,
                )
            else:
                self._snapshot = ProgressSnapshot(
                    stage=_copy(f"stage.{phase}", self.language),
                    confirmed=_copy(f"confirmed.{phase}", self.language),
                    next_action=_copy("next.success", self.language),
                    language=self.language,
                )
            return self._snapshot

    def cancelled(self) -> ProgressSnapshot:
        with self._lock:
            self._active_phases.clear()
            self._snapshot = ProgressSnapshot(
                stage=_copy("stage.stopped", self.language),
                confirmed=_copy("confirmed.no_work_started", self.language),
                next_action=_copy("next.retry_request", self.language),
                language=self.language,
            )
            return self._snapshot


def render_long_running(
    minutes: int,
    *,
    language: str | None = None,
    snapshot: ProgressSnapshot | None = None,
) -> str:
    """Render a localized heartbeat without internal implementation details.

    Discord passes its current semantic snapshot so the elapsed-time update
    stays inside the same ``stage / confirmed / next`` display contract. Other
    platforms retain a compact one-line heartbeat.
    """

    lang = language or (snapshot.language if snapshot is not None else None)
    elapsed = t(
        "gateway.progress.long_running",
        lang=lang,
        minutes=max(0, int(minutes)),
    )
    if snapshot is None:
        return elapsed
    return ProgressSnapshot(
        stage=snapshot.stage,
        # Preserve the last confirmed evidence instead of replacing it with a
        # timer.  The elapsed suffix guarantees a real Discord edit on every
        # heartbeat while keeping the evidence contract intact.
        confirmed=f"{snapshot.confirmed} · {elapsed}",
        next_action=snapshot.next_action,
        language=lang,
    ).render()


def render_busy_progress(
    minutes: int,
    *,
    language: str | None = None,
) -> str:
    """Render a safe busy-session acknowledgment for Discord.

    The active agent's iteration counter and tool name are deliberately not
    accepted as inputs.  Operators retain those diagnostics in logs, while a
    user follow-up gets the same localized three-field progress contract as the
    original run.
    """

    tracker = SemanticProgressTracker(language=language)
    active = tracker.tool_started("status")
    return render_long_running(minutes, language=tracker.language, snapshot=active)


def render_inactivity_warning(
    minutes: int,
    *,
    language: str | None = None,
    snapshot: ProgressSnapshot,
) -> str:
    """Render a localized warning without exposing provider/tool internals."""

    lang = language or snapshot.language or get_language()
    elapsed = t(
        "gateway.progress.long_running",
        lang=lang,
        minutes=max(0, int(minutes)),
    )
    return ProgressSnapshot(
        stage=_copy("stage.failure", lang),
        confirmed=f"{snapshot.confirmed} · {elapsed}",
        next_action=_copy("next.failure", lang),
        language=lang,
    ).render()


def render_inactivity_timeout(
    *,
    language: str | None = None,
    snapshot: ProgressSnapshot,
) -> str:
    """Render a localized terminal handoff while preserving known evidence."""

    lang = language or snapshot.language or get_language()
    return ProgressSnapshot(
        stage=_copy("stage.stopped", lang),
        confirmed=snapshot.confirmed,
        next_action=_copy("next.retry_request", lang),
        language=lang,
    ).render()
