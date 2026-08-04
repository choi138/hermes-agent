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
            "Delegate reasoning-heavy work to isolated subagents with independent "
            "context, terminal state, and tools. Use goal for one task or tasks for "
            "parallel work. Results arrive asynchronously as new messages: continue "
            "working and never wait/poll. Context must include paths, errors, constraints, "
            "and requested language because children cannot see this chat or clarify. "
            "Batch independent direct tool calls in one response; use execute_code only "
            "for filtering, branching, loops, or data-dependent sequencing. Work is not "
            "durable across /new or process exit; /stop cancels it. Treat summaries as "
            "unverified and verify external side effects from a URL, ID, path, or status. "
            "Leaf children cannot delegate, clarify, use memory/send_message, or "
            "execute_code; orchestrators may delegate only within role limits. Children "
            "inherit the parent model/fallback unless globally pinned."
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
        ("parameters", "properties", "delivery_mode"): (
            "Input delivery: background (default) avoids focus changes; foreground "
            "briefly fronts the app and requires approval. Escalate only after a result "
            "reports suspected_noop, background_unavailable, or recommends foreground."
        ),
        ("parameters", "properties", "bring_to_front"): (
            "With foreground delivery, keep the app frontmost after acting instead of "
            "restoring the prior app; default false."
        ),
        ("parameters", "properties", "pid"): (
            "Optional exact process target for capture; pair with window_id when needed."
        ),
        ("parameters", "properties", "window_id"): (
            "Optional exact native window target for capture; pair with pid when needed."
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
    "browser_cdp": {
        (): (
            "Send a raw Chrome DevTools Protocol command when a CDP endpoint is "
            "attached; prefer dedicated browser tools when they cover the operation. "
            "Omit target_id/frame_id for browser-level methods, use target_id for a "
            "tab, and use frame_id from browser_snapshot for cross-origin OOPIF work. "
            "Calls without frame_id are stateless. Look up method parameters in the "
            "official CDP reference when uncertain."
        ),
        ("parameters", "properties", "method"): (
            "CDP method, for example Target.getTargets or Runtime.evaluate."
        ),
        ("parameters", "properties", "params"): (
            "Method-specific JSON object; omit or use {} when there are no parameters."
        ),
        ("parameters", "properties", "target_id"): (
            "Optional tab targetId for page-level methods; mutually exclusive with frame_id."
        ),
        ("parameters", "properties", "frame_id"): (
            "Optional cross-origin OOPIF frame_id from browser_snapshot; use target_id "
            "for top-level tabs and normal DOM access for same-origin frames."
        ),
        ("parameters", "properties", "timeout"): (
            "Seconds to wait (default 30, maximum 300)."
        ),
    },
    "browser_dialog": {
        (): (
            "Accept or dismiss a native JavaScript dialog reported in "
            "browser_snapshot.pending_dialogs. Supply prompt_text for prompt(); when "
            "several dialogs are queued, select one with dialog_id. Available only "
            "with a CDP-capable browser."
        ),
        ("parameters", "properties", "action"): (
            "accept confirms (and permits beforeunload navigation); dismiss cancels."
        ),
        ("parameters", "properties", "prompt_text"): (
            "prompt() response; ignored by other dialog types."
        ),
        ("parameters", "properties", "dialog_id"): (
            "ID from browser_snapshot; needed only when multiple dialogs are queued."
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
            "Save compact stable facts across sessions. target=user is identity, "
            "preferences, and style; target=memory is environment, conventions, and "
            "lessons. Store durable preferences, corrections, and workflow facts—not "
            "task progress, raw dumps, rediscoverable facts, or procedures (use skills). "
            "For multiple changes or a full store, use one atomic operations batch to "
            "remove/shorten and add within the final character limit. Do not repeat a "
            "successful batch."
        ),
        ("parameters", "properties", "operations"): (
            "Atomic list of {action, content?, old_text?}; prefer for multiple changes "
            "or freeing space within the final character budget."
        ),
        ("parameters", "properties", "old_text"): (
            "For replace/remove, a short unique substring identifying the entry."
        ),
    },
    "execute_code": {
        (): (
            "Run Python with enabled Hermes wrappers imported from hermes_tools. Use "
            "only for filtering large results, branching, loops, or calls whose inputs "
            "depend on earlier outputs. For independent calls, request normal tools "
            "together in one response so they run concurrently. Wrappers return dicts; "
            "print the final result. Limit: 5 minutes, 50 calls, 50KB stdout."
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
            "Line limit (default 500, max 2000); pass 2000 to read a whole file in one call."
        ),
    },
    "write_file": {
        (): (
            "Write a complete file, creating parent directories. OVERWRITES the entire "
            "file; use patch for targeted edits or append. Suspicious large shrinks are "
            "blocked unless confirmed with the returned SHA-256. Runs syntax checks. "
            "On success the result's bytes_written is the resulting file size — report "
            "it instead of a follow-up wc/ls/stat call."
        ),
        ("parameters", "properties", "path"): (
            "File path to create or overwrite."
        ),
        ("parameters", "properties", "content"): "Complete replacement content.",
        ("parameters", "properties", "expected_sha256"): (
            "Current SHA-256 returned by a destructive-shrink refusal."
        ),
        ("parameters", "properties", "allow_destructive_overwrite"): (
            "Confirm an intentional shrink only with matching expected_sha256."
        ),
    },
    "patch": {
        (): (
            "Edit files with targeted replacement, atomic EOF append, or a V4A "
            "multi-file patch; returns a diff and runs syntax checks. replace needs "
            "path/old_string/new_string; append needs path/content; patch needs patch."
        ),
        ("parameters", "properties", "mode"): (
            "replace for targeted text, append for EOF additions, or patch for V4A."
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
        ("parameters", "properties", "content"): "Exact EOF content for append mode.",
        ("parameters", "properties", "expected_sha256"): (
            "Optional current SHA-256 guard for append."
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
            "Manage scheduled jobs. create requires schedule+prompt; no_agent=true "
            "requires script. Jobs start fresh without chat context, so prompts must be "
            "self-contained. Output auto-delivers; jobs cannot ask questions or create "
            "more jobs. List before remove; never guess IDs. Empty update values clear fields."
        ),
        ("parameters", "properties", "action"): (
            "Action; create needs schedule+prompt unless no_agent=true with script."
        ),
        ("parameters", "properties", "job_id"): (
            "Required for update/pause/resume/remove/run; get via list."
        ),
        ("parameters", "properties", "prompt"): (
            "Self-contained create prompt; skills load first."
        ),
        ("parameters", "properties", "schedule"): (
            "Create schedule: duration, every phrase, 5-field cron, or ISO time."
        ),
        ("parameters", "properties", "deliver"): (
            "Omit for the current thread. Set only if requested; "
            "platform:chat_id:thread_id preserves topic targeting."
        ),
        ("parameters", "properties", "skills"): (
            "Preloaded skills; [] clears."
        ),
        ("parameters", "properties", "model"): (
            "Optional model override; omitting provider pins the current provider."
        ),
        ("parameters", "properties", "script"): (
            "Per-tick script: stdout is prompt context, or delivered with no_agent. "
            "Relative paths use profile scripts; empty clears."
        ),
        ("parameters", "properties", "no_agent"): (
            "True requires script and skips the LLM; stdout is delivered verbatim, empty "
            "is silent, and failures alert. For deterministic jobs only."
        ),
        ("parameters", "properties", "context_from"): (
            "Latest completed outputs from job IDs; no same-tick wait; [] clears."
        ),
        ("parameters", "properties", "enabled_toolsets"): (
            "Agent toolset restriction; [] clears."
        ),
        ("parameters", "properties", "workdir"): (
            "Existing workdir scopes tools; same-dir jobs serialize; empty clears."
        ),
        ("parameters", "properties", "attach_to_session"): (
            "Continuable run context: dedicated thread or origin DM; origin only; "
            "overrides mirror delivery."
        ),
    },
    "text_to_speech": {
        (): (
            "Convert text to speech; returns a MEDIA path. Uses the configured "
            "voice and provider."
        ),
        ("parameters", "properties", "text"): (
            "Text to speak; long input may be truncated."
        ),
        ("parameters", "properties", "output_path"): "Optional output path.",
    },
    "terminal": {
        (): (
            "Run shell commands in a persistent environment; use file tools for reading, "
            "searching, and editing. The shell is for builds, git, processes, scripts, "
            "packages, and network work. Prefer foreground with a generous timeout for "
            "bounded jobs—it returns early on exit and avoids polling turns. Beyond the "
            "foreground limit, use background+notify_on_complete, then continue work or "
            "end the turn; never loop poll/wait. Silent background is only for servers/"
            "watchers. Do not use &, nohup, disown, or setsid. Verify readiness separately; "
            "use workdir for cwd and pty for interactive CLIs."
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
            "Rare mid-process signals for long-lived jobs only. Rate limit: one notice/"
            "15s; after three dropped windows, watching stops and exit notification "
            "takes over. For bounded jobs/end markers use notify_on_complete. Mutually "
            "exclusive with it."
        ),
        ("parameters", "properties", "timeout"): (
            "Maximum seconds (default 180; foreground max 600). Returns immediately on exit."
        ),
    },
    "process": {
        (): (
            "Manage terminal(background=true) processes. For bounded jobs, prefer "
            "notify_on_complete and continue work or end the turn; do not create short "
            "poll/wait loops. If the next step depends on the result, call wait once "
            "with timeout omitted; it uses the configured timeout, returns early on "
            "exit, and remains interruptible. Other actions inspect or control a process."
        ),
        ("parameters", "properties", "action"): (
            "Action. Avoid repeated short poll/wait calls for bounded jobs."
        ),
        ("parameters", "properties", "timeout"): (
            "wait only. Omit for the configured terminal timeout (180s by default); "
            "the call still returns immediately when the process exits."
        ),
    },
    "web_search": {
        (): (
            "Search the web and return titles, URLs, and snippets (five by default). "
            "Backend-supported operators such as site:, filetype:, intitle:, -term, "
            "and quoted phrases may be used."
        ),
        ("parameters", "properties", "query"): (
            "Search query, optionally with backend-supported operators."
        ),
        ("parameters", "properties", "limit"): (
            "Maximum results (default 5)."
        ),
    },
    "web_extract": {
        (): (
            "Extract up to five webpages or PDFs to markdown/text without LLM "
            "summarization. Content within char_limit is returned whole; longer content "
            "returns head+tail plus a saved path for read_file pagination. Images remain "
            "as placeholders/links. Use browser tools if extraction fails."
        ),
        ("parameters", "properties", "urls"): "URLs to extract (maximum five).",
        ("parameters", "properties", "char_limit"): (
            "Per-page inline character budget (default 15000); full text is saved when truncated."
        ),
    },
    "image_generate": {
        # Keep the root description: it is rebuilt for the configured backend and
        # is the authoritative statement of edit/reference-image capabilities.
        ("parameters", "properties", "prompt"): (
            "Detailed generation prompt or edit instruction."
        ),
        ("parameters", "properties", "aspect_ratio"): (
            "Output aspect ratio; landscape=16:9, portrait=9:16, square=1:1."
        ),
        ("parameters", "properties", "image_url"): (
            "Optional public URL or absolute conversation file path to edit; only "
            "available when the active model supports image editing."
        ),
        ("parameters", "properties", "reference_image_urls"): (
            "Optional style/character/composition references; support and count limit "
            "depend on the active model."
        ),
    },
    "read_terminal": {
        (): (
            "Read the Hermes desktop app's embedded terminal. With no arguments, return "
            "the visible screen and total_lines; use zero-based start_line and count to "
            "page through scrollback."
        ),
        ("parameters", "properties", "start_line"): (
            "Zero-based first line; omit for the visible screen."
        ),
        ("parameters", "properties", "count"): (
            "Lines to read; defaults to visible rows."
        ),
    },
    "close_terminal": {
        (): (
            "Close a Hermes desktop background-process terminal tab without stopping "
            "the process; output keeps buffering and the tab can be reopened. Use "
            "process(action='kill') to terminate instead."
        ),
        ("parameters", "properties", "process_id"): (
            "Background session ID returned by terminal or process(list)."
        ),
    },
    "todo": {
        (): (
            "Read or update this session's task list for multi-step work. Omit todos to "
            "read. Each item needs id, content, and pending/in_progress/completed/cancelled "
            "status; order is priority and only one may be in_progress. merge=false "
            "replaces the list, merge=true updates by id/adds items. Mark completed work "
            "promptly; cancel failures and add revised work. Returns the full list."
        ),
        ("parameters", "properties", "merge"): (
            "true updates/adds by id; false (default) replaces the list."
        ),
    },
    "skill_view": {
        (): (
            "Load a skill's SKILL.md and linked-file index, or pass file_path to read one "
            "of its references, templates, scripts, or assets. Use skills_list to find names."
        ),
        ("parameters", "properties", "name"): (
            "Skill name; plugin skills use qualified plugin:skill form."
        ),
        ("parameters", "properties", "file_path"): (
            "Optional linked path; omit for SKILL.md and its linked-file index."
        ),
    },
    "vision_analyze": {
        (): (
            "Load an image URL, local path, or data URL for analysis. Native-vision "
            "models receive the pixels next turn; otherwise an auxiliary vision model "
            "returns text. Use whenever the user or tool output references an image."
        ),
        ("parameters", "properties", "image_url"): (
            "HTTP(S) URL, local file path, or data URL."
        ),
        ("parameters", "properties", "question"): (
            "Specific question or requested analysis."
        ),
    },
    "stock_market_snapshot": {
        (): (
            "Fetch a current read-only daily snapshot for one explicit US/Korean ticker "
            "or supported index. This is informational market data, not trade execution."
        ),
        ("parameters", "properties", "symbol"): (
            "Ticker such as AAPL, BRK-B, 005930.KS, 035720.KQ, ^GSPC, or ^KS11."
        ),
    },
    "session_search": {
        (): (
            "Search/read historical Hermes sessions; never substitute history for an "
            "accessible live URL, file, account, app, or system. query does FTS discovery; "
            "session_id reads; add around_message_id to scroll; no args browses recent "
            "sessions. Discovery deduplicates sessions and includes kickoff/resolution "
            "plus a match window. Scroll with the first/last returned message ID. Resolve "
            "@session:<profile>/<id> with profile+session_id. FTS is AND by default and "
            "supports OR, quoted phrases, NOT, and prefix*."
        ),
        ("parameters", "properties", "query"): (
            "FTS query; omit to browse, ignored when scrolling."
        ),
        ("parameters", "properties", "limit"): (
            "Discovery limit (default 3, max 10)."
        ),
        ("parameters", "properties", "sort"): (
            "Discovery order: relevance (omit), newest, or oldest."
        ),
        ("parameters", "properties", "session_id"): (
            "Discovered session; alone reads, with around_message_id scrolls."
        ),
        ("parameters", "properties", "around_message_id"): (
            "Scroll anchor; reuse the last ID forward or first ID backward."
        ),
        ("parameters", "properties", "window"): (
            "Messages on each side of the scroll anchor, clamped to 1-20 (default 5)."
        ),
        ("parameters", "properties", "role_filter"): (
            "Comma-separated roles; default user,assistant. Add tool only when needed."
        ),
        ("parameters", "properties", "profile"): (
            "Read-only profile for @session:<profile>/<id>; omit for current."
        ),
    },
    "skill_manage": {
        (): (
            "Manage procedural skills and supporting files. Prefer patch for focused "
            "fixes and full edit for major rewrites. Create only for a proven reusable "
            "workflow or user request; keep triggers, exact steps, pitfalls, and checks. "
            "Confirm before create/delete. On delete, absorbed_into names the existing "
            "skill that received the content, or empty means pruning. Pinned skills may "
            "be patched/edited but not deleted."
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
            "Supporting references/templates/scripts/assets path; required for file "
            "write/remove, optional for patch (defaults to SKILL.md)."
        ),
        ("parameters", "properties", "absorbed_into"): (
            "Delete destination after consolidation, or empty for pruning."
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
