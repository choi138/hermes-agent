"""Stable gateway tool policies separated from Hermes identity profiles.

An identity profile chooses credentials, memory, sessions, and bot identity.
This module only narrows the immutable tool schema assembled for a session.
Keeping those concepts separate avoids creating synthetic Hermes profiles just
to obtain a smaller prompt and preserves per-conversation prompt caching.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from agent.request_footprint import (
    ToolSchemaMetrics,
    canonical_tool_schema_metrics,
)


DISCORD_CORE_SCHEMA_BUDGET_BYTES = 40_000


# Descriptions are the only fields compacted here.  Names, properties,
# required lists, enums, defaults, bounds, and permission-dependent dynamic
# schemas remain exactly as the tool registry produced them.  The full text is
# retained for CLI, workers, and explicitly authorized discord-ops sessions;
# normal Discord conversations pay for the concise contract on every turn.
_DISCORD_CORE_COMPACT_DESCRIPTIONS: dict[
    str, dict[tuple[str, ...], str]
] = {
    "delegate_task": {
        (): (
            "Delegate one or more reasoning-heavy tasks to isolated subagents. "
            "Provide goal for one task or tasks for parallel work; each child has "
            "independent context, terminal state, and tools, and returns an array "
            "entry asynchronously as a new message. Continue working—do not wait or "
            "poll. Put all paths, errors, constraints, and requested language/tone in "
            "context because children cannot see this conversation or ask via clarify. "
            "Use execute_code for mechanical workflows and direct tools for one call. "
            "Delegations are not durable: /new or process exit discards unfinished work, "
            "and /stop cancels it. Treat summaries as unverified self-reports; for "
            "external side effects require a URL, ID, absolute path, or status and verify "
            "it before claiming success. Leaf children cannot call delegate_task, "
            "clarify, memory, send_message, or execute_code; orchestrators may delegate "
            "only within the limits stated in role and retain the other restrictions. "
            "Children inherit the parent model/fallback unless globally pinned."
        ),
        ("parameters", "properties", "background"): (
            "Deprecated and ignored. Delegations already run in the background and "
            "return results as new messages; setting this has no effect."
        ),
    },
    "computer_use": {
        (): (
            "Control desktop apps in the background with screenshots, mouse, keyboard, "
            "scroll, and drag. Prefer capture mode='som', then target element indexes; "
            "use coordinates only when needed. Works on hidden or minimized windows "
            "without stealing focus. Requires cua-driver."
        ),
        ("parameters", "properties", "action"): (
            "Action to perform. capture is side-effect-free; every other action requires "
            "approval unless auto-approved. Use set_value for selects and sliders."
        ),
        ("parameters", "properties", "mode"): (
            "Capture mode: som (default) returns a screenshot, numbered elements, and AX; "
            "vision returns a screenshot; ax returns only the accessibility tree."
        ),
        ("parameters", "properties", "app"): (
            "Optional app name or bundle ID; omit for the frontmost window. Use screen or "
            "desktop for the OS shell. Capture one window or display at a time."
        ),
        ("parameters", "properties", "max_elements"): (
            "AX element cap (default 100, max 1000). Truncated results report totals; "
            "narrow with app or raise this. Applies to ax and image-missing fallbacks."
        ),
        ("parameters", "properties", "element"): (
            "1-based index from the latest capture(mode='som'); prefer it over coordinates."
        ),
        ("parameters", "properties", "coordinate"): (
            "Logical-screen [x,y] from capture; use only when no element index exists."
        ),
        ("parameters", "properties", "value"): (
            "set_value value: option label for selects, or numeric/string value for "
            "sliders and other AX-settable elements."
        ),
        ("parameters", "properties", "raise_window"): (
            "focus_app only: true raises the window and DISRUPTS the user; default false "
            "keeps input in the background."
        ),
        ("parameters", "properties", "capture_after"): (
            "Capture after the action to verify its effect in the same response."
        ),
    },
    "browser_navigate": {
        (): (
            "Open a URL and return compact snapshot refs. Call before other browser "
            "tools. Prefer lighter retrieval tools for plain content; use the browser "
            "for interaction and dynamic pages."
        ),
    },
    "browser_snapshot": {
        (): (
            "Refresh accessibility snapshot refs after interactions. full=false is "
            "compact; full=true includes page content. Requires navigate; long output "
            "may be truncated or summarized."
        ),
        ("parameters", "properties", "full"): (
            "Return complete page content instead of the compact interactive view."
        ),
    },
    "browser_click": {
        (): (
            "Click a snapshot ref such as @e5. Requires navigate and a current snapshot."
        ),
    },
    "browser_type": {
        (): (
            "Clear then type text into a snapshot ref. Requires navigate and a current "
            "snapshot."
        ),
    },
    "browser_scroll": {
        (): "Scroll up or down; requires navigate.",
    },
    "browser_back": {
        (): "Go back in browser history; requires navigate.",
    },
    "browser_press": {
        (): "Press a key or shortcut; requires navigate.",
    },
    "browser_get_images": {
        (): "List page image URLs and alt text; requires navigate.",
    },
    "browser_vision": {
        (): (
            "Capture a screenshot for visual inspection of CAPTCHAs, verification, or "
            "layout. Native-vision models receive it next turn; otherwise an auxiliary "
            "model analyzes it. Returns screenshot_path for MEDIA sharing. Requires "
            "navigate."
        ),
        ("parameters", "properties", "question"): (
            "Specific visual question to answer from the page."
        ),
        ("parameters", "properties", "annotate"): (
            "Overlay numbered elements; each label N maps to ref @eN."
        ),
    },
    "browser_console": {
        (): (
            "Read console messages and errors, optionally evaluating JavaScript for DOM "
            "or page-state inspection. Requires navigate."
        ),
        ("parameters", "properties", "expression"): (
            "Optional JavaScript evaluated with full page DOM/window access; the result "
            "is JSON-serialized."
        ),
    },
    "clarify": {
        (): (
            "Ask for clarification, feedback, or a meaningful decision. For selectable "
            "options, put up to four strings only in choices; never enumerate them in "
            "question because the UI renders choices as buttons and adds Other. Omit "
            "choices for open-ended input. Prefer a reasonable default for low-stakes "
            "decisions. Do not use for dangerous-command confirmation; terminal handles "
            "approval."
        ),
        ("parameters", "properties", "question"): (
            "Question text only; put selectable answers in choices."
        ),
        ("parameters", "properties", "choices"): (
            "Up to four selectable option strings; the UI adds Other. Omit only for "
            "open-ended free text."
        ),
    },
    "memory": {
        (): (
            "Save compact, stable facts across sessions. Prefer one atomic operations "
            "batch for multiple add/replace/remove changes; only its final result is "
            "checked against the character limit, so it can free space and add in one "
            "call. Use single fields only for one change. target=user stores identity, "
            "preferences, and style; target=memory stores environment, conventions, and "
            "lessons. Save durable preferences, corrections, and stable workflow facts; "
            "skip task progress, raw dumps, rediscoverable facts, and procedures (use "
            "skills). If full, batch removals or shortening with the new entry. Do not "
            "repeat a successful batch."
        ),
        ("parameters", "properties", "operations"): (
            "Atomic list of {action, content?, old_text?}; prefer for multiple changes "
            "or freeing space within the final character budget."
        ),
        ("parameters", "properties", "old_text"): (
            "For replace/remove, a short unique substring identifying the entry."
        ),
    },
    "read_file": {
        (): (
            "Read text with numbered lines and pagination; use instead of shell "
            "readers. Supports notebooks, DOCX, and XLSX; images and binaries require "
            "vision_analyze. Results over ~100K characters truncate at a line boundary "
            "and return next_offset; continue with offset."
        ),
        ("parameters", "properties", "path"): (
            "File path (absolute, relative, or ~/path)."
        ),
        ("parameters", "properties", "offset"): (
            "1-based starting line (default 1)."
        ),
        ("parameters", "properties", "limit"): (
            "Line limit (default 500, max 2000)."
        ),
    },
    "write_file": {
        (): (
            "Write content to a file, creating parent directories. OVERWRITES the "
            "entire file; use patch for targeted edits. Runs syntax checks and reports "
            "only errors introduced by this write."
        ),
        ("parameters", "properties", "path"): (
            "File path to create or overwrite."
        ),
        ("parameters", "properties", "content"): "Complete replacement content.",
    },
    "patch": {
        (): (
            "Edit files with fuzzy unique-string replacement or a V4A multi-file patch; "
            "returns a diff and runs syntax checks. replace mode needs "
            "mode/path/old_string/new_string; patch mode needs mode/patch."
        ),
        ("parameters", "properties", "mode"): (
            "replace for targeted text; patch for V4A content."
        ),
        ("parameters", "properties", "path"): "File path for replace mode.",
        ("parameters", "properties", "old_string"): (
            "Unique text to replace; include context unless replace_all=true."
        ),
        ("parameters", "properties", "new_string"): (
            "Replacement text; empty deletes the match."
        ),
        ("parameters", "properties", "replace_all"): (
            "Replace all matches; otherwise old_string must be unique."
        ),
        ("parameters", "properties", "patch"): "V4A content for patch mode.",
    },
    "search_files": {
        (): (
            "Search contents by regex or find files by glob via ripgrep. "
            "target=content returns matched lines, files, or counts; target=files lists "
            "paths by modification time. Use instead of shell grep, find, or ls."
        ),
        ("parameters", "properties", "pattern"): (
            "Regex for content or glob for files."
        ),
        ("parameters", "properties", "target"): (
            "content or files (default content)."
        ),
        ("parameters", "properties", "path"): (
            "Search root (default current directory)."
        ),
        ("parameters", "properties", "file_glob"): (
            "Content-search file filter glob."
        ),
        ("parameters", "properties", "limit"): "Result limit (default 50).",
        ("parameters", "properties", "offset"): "Results to skip (default 0).",
        ("parameters", "properties", "output_mode"): (
            "content, files_only, or count for content search."
        ),
        ("parameters", "properties", "context"): (
            "Context lines around content matches."
        ),
    },
    "cronjob": {
        (): (
            "Manage scheduled jobs. create requires schedule and prompt, except "
            "no_agent=true requires script. Jobs start fresh without current-chat "
            "context, so prompts must be self-contained; attached skills load in "
            "order first. Final output is delivered automatically and cron runs "
            "cannot ask questions or create more cron jobs. List before remove and "
            "use the returned job_id; never guess IDs. On update, skills=[] clears "
            "skills and empty strings clear optional string fields."
        ),
        ("parameters", "properties", "action"): (
            "create, list, update, pause, resume, remove, or run. create requires "
            "schedule and prompt unless no_agent=true with script."
        ),
        ("parameters", "properties", "job_id"): (
            "Required for update, pause, resume, remove, and run. Obtain it with "
            "list; never guess."
        ),
        ("parameters", "properties", "prompt"): (
            "Self-contained task instruction for create. Attached skills run first."
        ),
        ("parameters", "properties", "schedule"): (
            "Required for create; optional on update. Accepts durations ('30m'), "
            "every phrases ('every 2h'), five-field cron ('0 9 * * *'), or an ISO "
            "timestamp for a one-shot run."
        ),
        ("parameters", "properties", "deliver"): (
            "Omit to deliver to the current chat/thread (recommended). Set only when "
            "the user requests another target: origin, local (store only), all, or "
            "platform:chat_id:thread_id; comma-combine targets. Omitting thread_id "
            "loses topic targeting. all resolves connected channels at fire time."
        ),
        ("parameters", "properties", "skills"): (
            "Ordered skills loaded before the prompt. On update, [] clears them."
        ),
        ("parameters", "properties", "model"): (
            "Optional per-job model override. If provider is omitted, creation pins "
            "the current provider."
        ),
        ("parameters", "properties", "script"): (
            "Optional script run each tick. Normally stdout becomes prompt context; "
            "with no_agent=true it is the delivered result. Relative paths use the "
            "profile scripts directory; .sh/.bash use Bash, others Python. On update, "
            "an empty string clears it."
        ),
        ("parameters", "properties", "no_agent"): (
            "Default false. When true, script is required and prompt, skills, and "
            "model override are ignored: no LLM runs and stdout is delivered verbatim. "
            "Empty stdout is intentionally silent; non-zero exit or timeout sends an "
            "error alert. Use for fixed-output watchdogs/pollers, not work needing "
            "reasoning or summarization."
        ),
        ("parameters", "properties", "context_from"): (
            "Job ID or IDs whose latest completed output is added before this run. "
            "It does not wait for an upstream job running in the same tick. On update, "
            "[] clears the chain."
        ),
        ("parameters", "properties", "enabled_toolsets"): (
            "Optional toolsets available to this job's agent, such as web, terminal, "
            "file, or delegation. Omit for defaults; infer the minimum needed from the "
            "prompt. On update, [] clears the restriction."
        ),
        ("parameters", "properties", "workdir"): (
            "Optional existing absolute working directory. Its project instructions "
            "are loaded and terminal/file/code execution use it. Jobs with workdir run "
            "sequentially. On update, an empty string clears it."
        ),
        ("parameters", "properties", "attach_to_session"): (
            "Make delivery continuable with run context. Thread-capable platforms use "
            "a dedicated thread; DM-only platforms mirror into the origin session. "
            "Only the origin is affected, not fan-out targets; no effect for local "
            "delivery. Overrides cron.mirror_delivery for this job."
        ),
    },
    "terminal": {
        (): (
            "Run shell commands in a persistent session environment. Use dedicated "
            "file operations for reading, searching, editing, and creating files; use "
            "the shell for builds, installs, git, processes, scripts, packages, and "
            "network work. Foreground returns as soon as the command exits, even with "
            "a high timeout. For bounded work beyond the foreground limit, use "
            "background=true with notify_on_complete=true; silent background is only "
            "for long-lived servers/watchers. Do not wrap background work with &, "
            "nohup, disown, or setsid. Verify server readiness separately. Set workdir "
            "for cwd and pty=true for interactive CLIs."
        ),
        ("parameters", "properties", "background"): (
            "Run asynchronously. Bounded jobs must also set notify_on_complete=true; "
            "silent background is only for long-lived processes that do not exit. "
            "Prefer foreground for short commands."
        ),
        ("parameters", "properties", "notify_on_complete"): (
            "With background=true, notify exactly once on exit. Recommended for every "
            "bounded long task. Mutually exclusive with watch_patterns; this option "
            "wins if both are supplied."
        ),
        ("parameters", "properties", "watch_patterns"): (
            "Rare strings that trigger mid-process notifications for a long-lived "
            "process. At most one notice per 15 seconds; after three consecutive "
            "windows with dropped matches, watching stops and only exit is reported. "
            "Do not use for end markers, repeated errors, or bounded jobs—use "
            "notify_on_complete instead. Mutually exclusive with notify_on_complete."
        ),
    },
    "session_search": {
        (): (
            "Search or read Hermes conversation history from the session database. "
            "This is historical context, never current evidence about a URL, file, "
            "account, app/thread, contact, website, or live system; inspect a supplied "
            "source first when accessible, or state why it is inaccessible. Four "
            "shapes: query=FTS discovery; session_id+around_message_id=scroll; "
            "session_id alone=read; no args=browse recent sessions. Discovery returns "
            "deduplicated sessions with kickoff/resolution bookends and a +/-5 match "
            "window. Scroll by reusing the first/last message ID and increase window "
            "when needed. Resolve @session:<profile>/<id> with profile plus session_id. "
            "FTS requires all words by default; use OR, quoted phrases, NOT, or prefix*."
        ),
        ("parameters", "properties", "query"): (
            "FTS discovery query. Omit to browse recent sessions; ignored for scroll."
        ),
        ("parameters", "properties", "limit"): (
            "Discovery result limit (default 3, max 10). Use 5-10 for topics spanning "
            "several sessions."
        ),
        ("parameters", "properties", "sort"): (
            "Discovery ordering: omit for relevance, newest for recency questions, or "
            "oldest for origin questions. Ignored by read, scroll, and browse."
        ),
        ("parameters", "properties", "session_id"): (
            "Session returned by discovery. Alone reads it; pair with "
            "around_message_id to scroll."
        ),
        ("parameters", "properties", "around_message_id"): (
            "Scroll anchor. Use match_message_id, the last window ID to move forward, "
            "or the first to move backward."
        ),
        ("parameters", "properties", "window"): (
            "Messages on each side of the scroll anchor, clamped to 1-20 (default 5)."
        ),
        ("parameters", "properties", "role_filter"): (
            "Comma-separated roles. Discovery defaults to user,assistant; include tool "
            "only when tool output is relevant."
        ),
        ("parameters", "properties", "profile"): (
            "Read another Hermes profile's session database (read-only), especially "
            "for @session:<profile>/<id>. Omit for the current profile."
        ),
    },
    "skill_manage": {
        (): (
            "Create, patch, rewrite, delete, or add/remove supporting files in "
            "procedural skills. Prefer patch for focused fixes and full edit only for "
            "major rewrites. Create after a reusable non-trivial workflow succeeds or "
            "when the user asks; update stale instructions and newly found pitfalls. "
            "Confirm before create/delete. Good skills state triggers, exact steps, "
            "pitfalls, and verification. For delete, absorbed_into names an existing "
            "umbrella after content was merged, while an empty string means pruning; "
            "this preserves downstream references. Pinned skills cannot be deleted but "
            "can still be patched or edited."
        ),
        ("parameters", "properties", "name"): (
            "Skill name (lowercase, hyphens/underscores, max 64 chars); must exist for "
            "all actions except create."
        ),
        ("parameters", "properties", "content"): (
            "Complete SKILL.md including frontmatter. Required for create/edit; read "
            "the existing skill before a full edit."
        ),
        ("parameters", "properties", "old_string"): (
            "Unique text required for patch. Include enough context; set replace_all "
            "only for intentional multiple matches."
        ),
        ("parameters", "properties", "new_string"): (
            "Patch replacement; an empty string deletes the matched text."
        ),
        ("parameters", "properties", "replace_all"): (
            "For patch, replace every occurrence instead of requiring one unique match."
        ),
        ("parameters", "properties", "category"): (
            "Optional create-only category subdirectory, such as devops or mlops."
        ),
        ("parameters", "properties", "file_path"): (
            "Supporting path inside the skill. Required for write_file/remove_file and "
            "restricted to references, templates, scripts, or assets; optional for "
            "patch, which defaults to SKILL.md."
        ),
        ("parameters", "properties", "absorbed_into"): (
            "Delete intent: existing umbrella skill name after consolidation, or empty "
            "string for pruning without a target. Omission is backward-compatible but "
            "forces downstream reference handling to guess."
        ),
    },
}


@dataclass(frozen=True)
class GatewayToolPolicy:
    """Resolved, cache-stable policy for one gateway session."""

    name: str
    identity_profile: str
    enabled_toolsets: tuple[str, ...]


def schema_budget_bytes(policy_name: str) -> Optional[int]:
    """Return the measured deployment gate for a policy, if one applies."""

    if str(policy_name) == "discord-core":
        return DISCORD_CORE_SCHEMA_BUDGET_BYTES
    return None


def schema_within_budget(policy_name: str, metrics: ToolSchemaMetrics) -> bool:
    """Return whether a final schema satisfies its policy's byte budget."""

    budget = schema_budget_bytes(policy_name)
    return budget is None or metrics.json_bytes <= budget


def apply_gateway_tool_schema_policy(
    policy_name: str,
    tool_schemas: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the immutable schema surface selected by a gateway policy.

    Only ``discord-core`` uses concise descriptions.  Copy before editing so
    the process-wide ``model_tools`` schema cache, other platforms, workers,
    and discord-ops sessions retain the full definitions.
    """

    schemas = list(tool_schemas)
    if str(policy_name) != "discord-core":
        return schemas

    compacted: list[dict[str, Any]] = []
    for original in schemas:
        function = original.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        overrides = _DISCORD_CORE_COMPACT_DESCRIPTIONS.get(str(name))
        if not overrides:
            compacted.append(original)
            continue

        copied = deepcopy(original)
        copied_function = copied.get("function")
        if not isinstance(copied_function, dict):  # defensive malformed schema
            compacted.append(original)
            continue
        for path, description in overrides.items():
            node: Any = copied_function
            for component in path:
                if not isinstance(node, dict) or component not in node:
                    node = None
                    break
                node = node[component]
            if isinstance(node, dict):
                node["description"] = description
        compacted.append(copied)
    return compacted


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _kanban_is_configured(config: dict[str, Any], enabled: set[str]) -> bool:
    """Match the existing kanban tool's opt-in contract.

    ``tools.kanban_tools`` historically exposes lifecycle tools when the
    profile's top-level ``toolsets`` contains ``kanban``.  Also accept an
    explicit per-Discord entry so a platform-scoped configuration remains
    authoritative.  Requiring ``kanban`` in the already-resolved set preserves
    ``agent.disabled_toolsets`` as the final veto.
    """

    if "kanban" not in enabled:
        return False
    if "kanban" in _string_set(config.get("toolsets")):
        return True
    platform_toolsets = config.get("platform_toolsets")
    if isinstance(platform_toolsets, dict):
        return "kanban" in _string_set(platform_toolsets.get("discord"))
    return False


def _discord_ops_allowed(config: dict[str, Any], source: Any) -> bool:
    """Require an exact user *and* channel allowlist match for full Kanban.

    Wildcards are deliberately not supported.  The normal gateway admission
    check still runs first; this is an additional least-privilege gate for the
    large board-routing surface.
    """

    kanban_cfg = config.get("kanban")
    if not isinstance(kanban_cfg, dict):
        return False
    allowed_users = _string_set(kanban_cfg.get("discord_ops_users"))
    allowed_channels = _string_set(kanban_cfg.get("discord_ops_channels"))
    if not allowed_users or not allowed_channels:
        return False
    if "*" in allowed_users or "*" in allowed_channels:
        return False
    user_id = str(getattr(source, "user_id", "") or "")
    source_channels = {
        str(value)
        for value in (
            getattr(source, "chat_id", None),
            getattr(source, "thread_id", None),
            getattr(source, "parent_chat_id", None),
        )
        if value
    }
    return user_id in allowed_users and bool(source_channels & allowed_channels)


def resolve_gateway_tool_policy(
    config: dict[str, Any],
    *,
    platform: str,
    source: Any,
    identity_profile: str,
    enabled_toolsets: Iterable[str],
    disabled_toolsets: Iterable[str] = (),
) -> GatewayToolPolicy:
    """Return the fixed policy/toolset tuple for a gateway session.

    Discord profiles that opted into Kanban get one asynchronous intake tool.
    Dispatcher workers keep their task-scoped lifecycle policy in
    :mod:`model_tools`; exact user+channel operator allowlists retain the full
    orchestrator surface.  Other platform behavior is unchanged.
    """

    enabled = {str(item) for item in enabled_toolsets if str(item)}
    disabled = {str(item) for item in disabled_toolsets if str(item)}
    profile = str(identity_profile or "default")
    if str(platform) != "discord":
        return GatewayToolPolicy(
            name="platform-default",
            identity_profile=profile,
            enabled_toolsets=tuple(sorted(enabled)),
        )

    configured = "kanban" not in disabled and _kanban_is_configured(config, enabled)
    if configured and _discord_ops_allowed(config, source):
        return GatewayToolPolicy(
            name="discord-ops",
            identity_profile=profile,
            enabled_toolsets=tuple(sorted(enabled)),
        )

    # A normal Discord model must never receive the worker/orchestrator
    # lifecycle surface.  Replace it once, before AIAgent construction, so the
    # schema stays byte-stable for the life of the cached conversation.
    enabled.discard("kanban")
    enabled.discard("kanban_worker")
    enabled.discard("kanban_submit")
    if configured:
        enabled.add("kanban_submit")
    return GatewayToolPolicy(
        name="discord-core",
        identity_profile=profile,
        enabled_toolsets=tuple(sorted(enabled)),
    )
