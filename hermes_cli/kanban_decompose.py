"""Kanban decomposer — fan a triage task out into a graph of child tasks.

Invoked by ``hermes kanban decompose [task_id | --all]`` and the
auto-decompose path in the gateway dispatcher loop. Reads the user's
profile roster (with descriptions) and asks the auxiliary LLM to
return a task graph in JSON. Then atomically creates the children,
links them under the root, and flips the root ``triage -> todo``.

The root task stays alive and becomes the parent of every leaf child,
so when the whole graph completes the root wakes back up — its
assignee (the orchestrator profile) gets a chance to judge completion
and add more tasks if the work isn't done yet.

Design notes
------------

* Mirrors the shape of ``hermes_cli/kanban_specify.py``: lazy aux
  client import inside the function, lenient response parse, never
  raises on expected failure modes.

* The system prompt sees the *configured* profile roster — names plus
  descriptions plus the default fallback. Profiles without a
  description are still listed (with a note) so the decomposer can
  match on name as a fallback, but the user has an obvious incentive
  to describe them.

* ``fanout=false`` collapses to the same effect as ``kanban specify``:
  we tighten the body and flip ``triage -> todo`` as a single task,
  no children created. This makes ``decompose`` a strict superset of
  ``specify`` from the user's perspective.

* If the LLM picks an assignee that doesn't exist as a profile, we
  rewrite it to the configured ``default_assignee`` (or the default
  profile if unset). A child task NEVER ends up with ``assignee=None``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli import profiles as profiles_mod

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are the Kanban decomposer for the Hermes Agent board.

A user dropped a rough idea into the Triage column. First decide whether it
needs durable coordination at all. One-owner synchronous deterministic work
without durability, waiting, approval, or independent-role requirements must
stay direct as one tightened task. Only fan out when the board provides a real
coordination benefit, then route each card to the best matching profile.

You will be given:
  - The original task title and body
  - The list of available profiles (each with name + description)
  - The fallback "default_assignee" used when no profile fits

Output a single JSON object with this exact shape:

  {
    "fanout": true,
    "rationale": "<one sentence on why durable coordination is required>",
    "coordination_reasons": [
      "<approval|durable_handoff|independent_qa|parallel_specialists|waiting>"
    ],
    "tasks": [
      {
        "title": "<concrete task title, imperative voice, <= 80 chars>",
        "body":  "<detailed spec for the worker on this child task>",
        "assignee": "<profile name from the roster, or null for default>",
        "parents": [<int>, ...],
        "role": "<approval|final_owner|implementation|independent_qa|specialist|waiting>"
      },
      ...
    ]
  }

Rules:
  - "parents" is a list of INDICES (0-based) into this same "tasks" list,
    expressing actual data dependencies. Tasks with no parents run in
    PARALLEL. Tasks with parents wait until every parent completes.
  - Fanout requires at least one concrete coordination_reasons value and the
    graph must actually implement every listed reason. Never invent a reason
    merely to split a direct task.
  - Keep the initial graph at no more than three dependency STAGES. There is no
    card-count cap: parallel specialists may share a stage when quality needs it.
  - Do not pre-create correction, re-QA, fallback, replacement, recovery,
    observer, or duplicate-final cards. Add remediation only after verified need.
  - Preserve independent QA when the user explicitly requests it. Mark the
    cards with implementation/independent_qa roles, make QA depend on the
    implementation artifact, and assign QA to a distinct profile.
  - Pick assignees from the roster by matching the task to the profile's
    DESCRIPTION (not just the name). When nothing matches well, use null
    and the system will route to the default_assignee.
  - Each child task body is what a fresh worker will read with no other
    context — be specific about goal, approach, and acceptance criteria.

When the task is genuinely a single unit of work (no useful decomposition),
return:

  {
    "fanout": false,
    "rationale": "<one sentence>",
    "title": "<tightened title>",
    "body":  "<concrete spec for a single worker>",
    "assignee": "<profile name from the roster, or null for default>"
  }

In that case the task stays as one work item, just with a tightened spec and
a concrete assignee. If no profile fits, use null and the system will route to
the default_assignee.

No preamble, no closing remarks, no code fences. Output only the JSON object.
"""


_USER_TEMPLATE = """Task id: {task_id}
Title: {title}
Body:
{body}

Available profiles (assignees you may pick from):
{roster}

Default assignee (used when no profile fits a task): {default_assignee}
"""


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_INDEPENDENT_QA_RE = re.compile(
    r"(?:\bindependent(?: pre commit)? "
    r"(?:qa|review|reviewer|verification|verifier)\b|"
    r"\b(?:review|reviewed|verify|verified) independently\b|"
    r"\bindependently (?:reviewed|verified)\b|"
    r"\b(?:separate|different) (?:qa|reviewer)\b|"
    r"\bhave someone else (?:review|verify)\b|"
    r"\breviewer must be (?:independent|different)\b|"
    r"\b(?:review|reviewed|verify|verified|verification) by "
    r"(?:an? )?(?:(?:independent|different) (?:reviewer|assignee|person)|"
    r"someone else)\b|독립(?:적인)?\s*(?:qa|검증|리뷰))",
    re.IGNORECASE,
)
_INDEPENDENT_QA_NEGATION_RE = re.compile(
    r"(?:\bno (?:independent|separate) "
    r"(?:qa|review|reviewer|verification)(?: is)? "
    r"(?:required|needed|necessary)\b|"
    r"\bindependent (?:qa|review|reviewer|verification) is not "
    r"(?:required|needed|necessary)\b|"
    r"\bdo not (?:assign|add|request|require|use) "
    r"(?:an? )?(?:independent|separate|different) "
    r"(?:qa|review|reviewer|verification)\b)",
    re.IGNORECASE,
)
_COORDINATION_REASONS = frozenset(
    {
        "approval",
        "durable_handoff",
        "independent_qa",
        "parallel_specialists",
        "waiting",
    }
)
_TASK_ROLES = frozenset(
    {
        "approval",
        "final_owner",
        "implementation",
        "independent_qa",
        "specialist",
        "waiting",
    }
)


@dataclass
class DecomposeOutcome:
    """Result of decomposing a single triage task."""

    task_id: str
    ok: bool
    reason: str = ""
    fanout: bool = False
    child_ids: list[str] | None = None
    new_title: Optional[str] = None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _extract_json_blob(raw: str) -> Optional[dict]:
    if not raw:
        return None
    stripped = _FENCE_RE.sub("", raw.strip())
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    candidate = stripped[first : last + 1]
    try:
        val = json.loads(candidate)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(val, dict):
        return None
    return val


def _profile_author() -> str:
    """Mirror of ``hermes_cli.kanban._profile_author``."""
    return (
        os.environ.get("HERMES_PROFILE")
        or os.environ.get("USER")
        or "decomposer"
    )


def _load_config() -> dict:
    try:
        from hermes_cli.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def _resolve_orchestrator_profile(cfg: dict) -> str:
    """Resolve which profile owns the root/orchestration task after fan-out.

    Falls back to the active default profile when ``kanban.orchestrator_profile``
    is unset, so a task is never stranded for lack of an orchestrator.
    """
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    explicit = (kanban_cfg.get("orchestrator_profile") or "").strip()
    if explicit:
        try:
            if profiles_mod.profile_exists(explicit):
                return explicit
        except Exception:
            pass
    # Fall back to the active default profile.
    try:
        return profiles_mod.get_active_profile_name() or "default"
    except Exception:
        return "default"


def _resolve_default_assignee(cfg: dict) -> str:
    """Resolve which profile catches child tasks the orchestrator can't route."""
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    explicit = (kanban_cfg.get("default_assignee") or "").strip()
    if explicit:
        try:
            if profiles_mod.profile_exists(explicit):
                return explicit
        except Exception:
            pass
    try:
        return profiles_mod.get_active_profile_name() or "default"
    except Exception:
        return "default"


def _build_roster() -> tuple[list[dict], set[str]]:
    """Return (roster_for_prompt, valid_assignee_names).

    Each roster entry is ``{name, description, has_description}``. The
    valid-set is used after the LLM responds to rewrite invalid
    assignees to the default fallback.
    """
    roster: list[dict] = []
    valid: set[str] = set()
    try:
        all_profiles = profiles_mod.list_profiles()
    except Exception as exc:
        logger.warning("decompose: failed to list profiles: %s", exc)
        return roster, valid
    for p in all_profiles:
        desc = (p.description or "").strip()
        roster.append({
            "name": p.name,
            "description": desc or f"(no description; profile named {p.name!r})",
            "has_description": bool(desc),
        })
        valid.add(p.name)
    return roster, valid


def _format_roster(roster: list[dict]) -> str:
    if not roster:
        return "  (no profiles installed — decomposer cannot route work)"
    lines = []
    for entry in roster:
        tag = "" if entry["has_description"] else " ⚠ undescribed"
        lines.append(f"  - {entry['name']}{tag}: {entry['description']}")
    return "\n".join(lines)


def _task_requires_independent_qa(task: kb.Task) -> bool:
    text = "\n".join(part for part in (task.title, task.body or "") if part)
    clauses = re.split(r"(?:[.!?;\n]+|\bbut\b)", text, flags=re.IGNORECASE)
    for clause in clauses:
        normalized = re.sub(r"[\W_]+", " ", clause.casefold()).strip()
        if not normalized or _INDEPENDENT_QA_NEGATION_RE.search(normalized):
            continue
        if _INDEPENDENT_QA_RE.search(normalized):
            return True
    return False


def _normalize_assignee_choice(
    assignee: object,
    *,
    default_assignee: str,
    valid_names: set[str],
) -> str:
    """Return a valid assignee, falling back to ``default_assignee``.

    Fan-out children and the single-task fallback should share the same
    routing guarantee: promoted work must not be left unassigned.
    """
    if not isinstance(assignee, str) or not assignee.strip():
        return default_assignee
    chosen = assignee.strip()
    if chosen not in valid_names:
        return default_assignee
    return chosen


def decompose_task(
    task_id: str,
    *,
    author: Optional[str] = None,
    timeout: Optional[int] = None,
) -> DecomposeOutcome:
    """Decompose a triage task into a graph of child tasks.

    Returns an outcome describing what happened. Never raises for
    expected failure modes (task not in triage, no aux client
    configured, API error, malformed response, decomposer returned
    fanout=true with empty task list) — those surface via ``ok=False``.
    """
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    if task is None:
        return DecomposeOutcome(task_id, False, "unknown task id")
    if task.status != "triage":
        return DecomposeOutcome(
            task_id, False, f"task is not in triage (status={task.status!r})"
        )

    cfg = _load_config()
    orchestrator = _resolve_orchestrator_profile(cfg)
    default_assignee = _resolve_default_assignee(cfg)
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    auto_promote = bool(kanban_cfg.get("auto_promote_children", True))
    roster, valid_names = _build_roster()

    try:
        from agent.auxiliary_client import call_llm  # type: ignore
    except Exception as exc:
        logger.debug("decompose: auxiliary client import failed: %s", exc)
        return DecomposeOutcome(task_id, False, "auxiliary client unavailable")

    user_msg = _USER_TEMPLATE.format(
        task_id=task.id,
        title=_truncate(task.title or "", 400),
        body=_truncate(task.body or "(no body)", 4000),
        roster=_format_roster(roster),
        default_assignee=default_assignee,
    )

    try:
        # Route through call_llm so auxiliary.kanban_decomposer.* config
        # (provider/model/base_url, extra_body, reasoning_effort, retries)
        # all apply — the previous direct client.chat.completions.create()
        # path dropped auxiliary.<task>.extra_body entirely (#35566).
        resp = call_llm(
            task="kanban_decomposer",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=4000,
            timeout=timeout or 180,
        )
    except Exception as exc:
        logger.info(
            "decompose: API call failed for %s (%s)", task_id, exc,
        )
        return DecomposeOutcome(task_id, False, f"LLM error: {type(exc).__name__}")

    try:
        raw = resp.choices[0].message.content or ""
    except Exception:
        raw = ""

    parsed = _extract_json_blob(raw)
    if parsed is None:
        return DecomposeOutcome(task_id, False, "LLM returned malformed JSON")

    raw_fanout = parsed.get("fanout")
    if not isinstance(raw_fanout, bool):
        return DecomposeOutcome(
            task_id,
            False,
            "decomposer field fanout must be a boolean",
        )
    fanout = raw_fanout
    audit_author = author or _profile_author()
    independent_qa_required = _task_requires_independent_qa(task)

    def _promote_direct(
        *,
        reason: str,
        use_model_fields: bool,
    ) -> DecomposeOutcome:
        if use_model_fields:
            raw_title = parsed.get("title")
            raw_body = parsed.get("body")
            title_val = (
                raw_title.strip()
                if isinstance(raw_title, str) and raw_title.strip()
                else None
            )
            body_val = (
                raw_body
                if isinstance(raw_body, str) and raw_body.strip()
                else None
            )
            requested_assignee = parsed.get("assignee")
        else:
            title_val = task.title
            body_val = task.body if task.body and task.body.strip() else None
            requested_assignee = task.assignee
        assignee_val = None
        if not task.assignee:
            assignee_val = _normalize_assignee_choice(
                requested_assignee,
                default_assignee=default_assignee,
                valid_names=valid_names,
            )
        if title_val is None and body_val is None:
            return DecomposeOutcome(
                task_id,
                False,
                "decomposer returned a direct task with no title/body",
            )
        with kb.connect_closing() as conn:
            promoted = kb.specify_triage_task(
                conn,
                task_id,
                title=title_val,
                body=body_val,
                assignee=assignee_val,
                author=audit_author,
            )
        if not promoted:
            return DecomposeOutcome(
                task_id,
                False,
                "task moved out of triage before promotion",
            )
        return DecomposeOutcome(
            task_id,
            True,
            reason,
            fanout=False,
            new_title=title_val,
        )

    if not fanout:
        if independent_qa_required:
            return DecomposeOutcome(
                task_id,
                False,
                "explicit independent QA requires implementation and reviewer cards",
            )
        return _promote_direct(
            reason="single task (no fanout)",
            use_model_fields=True,
        )

    raw_tasks = parsed.get("tasks") or []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return DecomposeOutcome(
            task_id, False, "decomposer returned fanout=true with empty tasks list",
        )

    raw_reasons = parsed.get("coordination_reasons")
    reasons_valid = (
        isinstance(raw_reasons, list)
        and bool(raw_reasons)
        and all(
            isinstance(reason, str) and reason in _COORDINATION_REASONS
            for reason in raw_reasons
        )
    )
    if not reasons_valid:
        if independent_qa_required:
            return DecomposeOutcome(
                task_id,
                False,
                "explicit independent QA requires coordination_reasons=independent_qa",
            )
        return _promote_direct(
            reason="direct-first fallback: no valid coordination reason",
            use_model_fields=False,
        )
    coordination_reasons = sorted(set(raw_reasons))
    if len(raw_tasks) < 2:
        if independent_qa_required:
            return DecomposeOutcome(
                task_id,
                False,
                "explicit independent QA requires at least two cards",
            )
        return _promote_direct(
            reason="direct-first fallback: fanout requires at least two cards",
            use_model_fields=False,
        )

    # Rewrite invalid assignees to the default fallback. Never leave a
    # task with assignee=None — the user explicitly does not want that.
    children: list[dict] = []
    for idx, entry in enumerate(raw_tasks):
        if not isinstance(entry, dict):
            return DecomposeOutcome(
                task_id, False, f"tasks[{idx}] is not an object",
            )
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            return DecomposeOutcome(
                task_id, False, f"tasks[{idx}].title is missing or empty",
            )
        body = entry.get("body")
        if not isinstance(body, str):
            body = ""
        assignee = entry.get("assignee")
        chosen = _normalize_assignee_choice(
            assignee,
            default_assignee=default_assignee,
            valid_names=valid_names,
        )
        if (
            isinstance(assignee, str)
            and assignee.strip()
            and assignee.strip() not in valid_names
        ):
            logger.info(
                "decompose: task %s child %d picked unknown assignee %r — "
                "routing to default_assignee %r",
                task_id, idx, assignee, default_assignee,
            )
        role = entry.get("role")
        if role is not None and (
            not isinstance(role, str) or role not in _TASK_ROLES
        ):
            return DecomposeOutcome(
                task_id,
                False,
                f"tasks[{idx}].role is not a supported coordination role",
            )
        parents = entry.get("parents") or []
        if not isinstance(parents, list):
            return DecomposeOutcome(
                task_id,
                False,
                f"tasks[{idx}].parents must be a list",
            )
        clean_parents: list[int] = []
        for parent in parents:
            if type(parent) is not int:
                return DecomposeOutcome(
                    task_id,
                    False,
                    f"tasks[{idx}] parent index must be an integer",
                )
            if parent < 0 or parent >= len(raw_tasks):
                return DecomposeOutcome(
                    task_id,
                    False,
                    f"tasks[{idx}] parent index {parent} is out of range",
                )
            if parent == idx:
                return DecomposeOutcome(
                    task_id,
                    False,
                    f"tasks[{idx}] cannot list itself as a parent",
                )
            clean_parents.append(parent)
        children.append({
            "title": title.strip()[:200],
            "body": body.strip(),
            "assignee": chosen,
            "parents": clean_parents,
            "role": role,
        })

    if independent_qa_required and "independent_qa" not in coordination_reasons:
        return DecomposeOutcome(
            task_id,
            False,
            "explicit independent QA must declare coordination_reasons=independent_qa",
        )

    if "parallel_specialists" in coordination_reasons:
        root_children = [child for child in children if not child["parents"]]
        root_assignees = {child["assignee"] for child in root_children}
        if len(root_children) < 2 or len(root_assignees) < 2:
            return DecomposeOutcome(
                task_id,
                False,
                "parallel_specialists requires parallel roots with distinct assignees",
            )

    if "durable_handoff" in coordination_reasons:
        has_cross_assignee_edge = any(
            child["assignee"] != children[parent]["assignee"]
            for child in children
            for parent in child["parents"]
        )
        if not has_cross_assignee_edge:
            return DecomposeOutcome(
                task_id,
                False,
                "durable_handoff requires a dependency edge across assignees",
            )

    for reason, required_role in (("waiting", "waiting"), ("approval", "approval")):
        if reason in coordination_reasons and not any(
            child["role"] == required_role for child in children
        ):
            return DecomposeOutcome(
                task_id,
                False,
                f"{reason} coordination requires a {required_role} role card",
            )

    if "independent_qa" in coordination_reasons:
        implementation_indices = {
            idx
            for idx, child in enumerate(children)
            if child["role"] == "implementation"
        }
        qa_indices = [
            idx
            for idx, child in enumerate(children)
            if child["role"] == "independent_qa"
        ]
        if not implementation_indices or not qa_indices:
            return DecomposeOutcome(
                task_id,
                False,
                "independent QA requires implementation and independent_qa roles",
            )
        implementation_assignees = {
            children[idx]["assignee"] for idx in implementation_indices
        }
        if any(
            children[idx]["assignee"] in implementation_assignees
            for idx in qa_indices
        ):
            return DecomposeOutcome(
                task_id,
                False,
                "independent QA requires a distinct assignee from implementation",
            )
        if any(
            not implementation_indices.intersection(children[idx]["parents"])
            for idx in qa_indices
        ):
            return DecomposeOutcome(
                task_id,
                False,
                "independent QA must depend on an implementation card",
            )
        qa_covered_implementations: set[int] = set()
        for qa_idx in qa_indices:
            ancestors: set[int] = set()
            pending = list(children[qa_idx]["parents"])
            while pending:
                parent_idx = pending.pop()
                if parent_idx in ancestors:
                    continue
                ancestors.add(parent_idx)
                pending.extend(children[parent_idx]["parents"])
            qa_covered_implementations.update(
                ancestors.intersection(implementation_indices)
            )
        if qa_covered_implementations != implementation_indices:
            return DecomposeOutcome(
                task_id,
                False,
                "independent QA must cover every implementation card",
            )

    try:
        with kb.connect_closing() as conn:
            child_ids = kb.decompose_triage_task(
                conn,
                task_id,
                root_assignee=orchestrator,
                children=children,
                author=audit_author,
                auto_promote=auto_promote,
                coordination_reasons=coordination_reasons,
            )
    except ValueError as exc:
        return DecomposeOutcome(task_id, False, f"DB rejected graph: {exc}")
    except Exception as exc:
        logger.exception("decompose: DB error on task %s", task_id)
        return DecomposeOutcome(task_id, False, f"DB error: {type(exc).__name__}")

    if child_ids is None:
        return DecomposeOutcome(
            task_id, False, "task moved out of triage before decomposition",
        )

    return DecomposeOutcome(
        task_id, True, f"decomposed into {len(child_ids)} children",
        fanout=True, child_ids=child_ids,
    )


def list_triage_ids(*, tenant: Optional[str] = None) -> list[str]:
    """Return task ids currently in the triage column."""
    with kb.connect_closing() as conn:
        rows = kb.list_tasks(
            conn,
            status="triage",
            tenant=tenant,
            limit=1000,
        )
    return [row.id for row in rows]
