"""Tests for the decomposer module + `hermes kanban decompose` CLI surface.

The auxiliary LLM client is mocked — no network calls. Tests exercise the
prompt plumbing, response parsing, DB writes (via the real DB helper),
and the assignee-fallback logic.
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose as decomp


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_aux_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _patch_aux_client(content: str, *, model: str = "test-model"):
    # decompose_task now routes through call_llm (see #35566) — mock it at
    # the source module so task config, extra_body, and retries stay out of
    # unit-test scope.
    return patch(
        "agent.auxiliary_client.call_llm",
        return_value=_fake_aux_response(content),
    )


def _patch_extra_body():
    # No-op shim retained for call-site compatibility: extra_body plumbing
    # now lives inside call_llm, which _patch_aux_client already mocks.
    return patch("agent.auxiliary_client.get_auxiliary_extra_body", return_value={})


def _patch_list_profiles(names: list[str]):
    """Pretend the named profiles exist. The decomposer uses
    profiles_mod.list_profiles() to build the roster + valid-set, and
    profiles_mod.profile_exists() to resolve orchestrator/default."""
    from types import SimpleNamespace
    fake_profiles = [
        SimpleNamespace(
            name=n, is_default=(i == 0), description=f"desc for {n}",
            description_auto=False, model="m", provider="p", skill_count=1,
        )
        for i, n in enumerate(names)
    ]
    return [
        patch("hermes_cli.profiles.list_profiles", return_value=fake_profiles),
        patch("hermes_cli.profiles.profile_exists", side_effect=lambda x: x in names),
        patch("hermes_cli.profiles.get_active_profile_name", return_value=names[0] if names else "default"),
    ]


def test_decompose_with_fanout_creates_children(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ship a feature", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "coordination_reasons": ["durable_handoff"],
        "tasks": [
            {
                "title": "research",
                "body": "look it up",
                "assignee": "researcher",
                "parents": [],
                "role": "implementation",
            },
            {
                "title": "build",
                "body": "code it",
                "assignee": "engineer",
                "parents": [0],
                "role": "final_owner",
            },
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "researcher", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    assert outcome.child_ids and len(outcome.child_ids) == 2

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert root.status == "todo"
    assert c0.status == "ready"
    assert c1.status == "todo"
    assert c0.assignee == "researcher"
    assert c1.assignee == "engineer"


def test_decompose_fanout_false_assigns_default_when_unassigned(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="just one thing", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "**Goal**\nDo the thing.",
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is False
    assert outcome.new_title == "Tightened title"
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    # specify path with no parents -> recompute_ready flips to 'ready'
    assert task.status == "ready"
    assert task.title == "Tightened title"
    assert task.assignee == "fallback"


@pytest.mark.parametrize("invalid_fanout", ["false", 1, {}, None])
def test_decompose_rejects_non_boolean_fanout(kanban_home, invalid_fanout):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="keep in triage", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": invalid_fanout,
        "title": "must not be promoted",
        "body": "invalid model schema",
        "tasks": [],
    })
    patches = _patch_list_profiles(["orchestrator"])
    for patcher in patches:
        patcher.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for patcher in patches:
            patcher.stop()

    assert outcome.ok is False
    assert "fanout must be a boolean" in (outcome.reason or "")
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "triage"


def test_decompose_policy_recommends_direct_for_single_owner_sync_work(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rename one deterministic symbol", triage=True)

    payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "one owner can finish synchronously",
        "title": "Rename deterministic symbol",
        "body": "Rename it and run the focused test.",
        "assignee": "engineer",
    })
    patches = _patch_list_profiles(["orchestrator", "engineer"])
    for patcher in patches:
        patcher.start()
    try:
        with patch(
            "agent.auxiliary_client.call_llm",
            return_value=_fake_aux_response(payload),
        ) as call_llm, _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for patcher in patches:
            patcher.stop()

    system_prompt = call_llm.call_args.kwargs["messages"][0]["content"]
    assert "one-owner synchronous deterministic work" in system_prompt.lower()
    assert "durability, waiting, approval, or independent-role" in system_prompt
    assert outcome.ok is True
    assert outcome.fanout is False


def test_decompose_runtime_falls_back_to_direct_without_coordination_reason(
    kanban_home,
):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="rename one deterministic symbol",
            body="One owner can rename it and run the focused test.",
            triage=True,
        )

    payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "unnecessary split",
        "tasks": [
            {
                "title": "rename symbol",
                "body": "rename it",
                "assignee": "engineer",
                "parents": [],
            },
            {
                "title": "run one test",
                "body": "run it",
                "assignee": "reviewer",
                "parents": [0],
            },
        ],
    })
    patches = _patch_list_profiles(["orchestrator", "engineer", "reviewer"])
    for patcher in patches:
        patcher.start()
    try:
        with _patch_aux_client(payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for patcher in patches:
            patcher.stop()

    assert outcome.ok is True
    assert outcome.fanout is False
    assert "direct-first" in outcome.reason
    assert not outcome.child_ids
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        child_count = conn.execute(
            "SELECT COUNT(*) FROM task_links WHERE child_id=?",
            (tid,),
        ).fetchone()[0]
    assert task is not None
    assert task.status == "ready"
    assert child_count == 0


@pytest.mark.parametrize(
    "title",
    [
        "Add parser support with independent QA",
        "Parser implementation with independent QA",
        "Assign an independent reviewer",
        "Have it independently reviewed",
        "Review by a different assignee",
        "Review_by_a_different_assignee",
        "Reviewed by someone else",
    ],
)
def test_explicit_independent_qa_phrasings_require_qa_invariant(title):
    task = MagicMock(title=title, body=None)
    assert decomp._task_requires_independent_qa(task) is True


@pytest.mark.parametrize(
    "title",
    [
        "Independent pre-commit review",
        "Verify independently before merge",
        "Run separate QA",
        "Have someone else review the patch",
    ],
)
def test_additional_explicit_independent_qa_phrasings_require_qa(title):
    task = MagicMock(title=title, body=None)
    assert decomp._task_requires_independent_qa(task) is True


@pytest.mark.parametrize(
    "title",
    [
        "No independent QA is required",
        "Independent QA is not needed",
        "Do not assign a separate reviewer",
    ],
)
def test_negated_independent_qa_phrasings_remain_direct(title):
    task = MagicMock(title=title, body=None)
    assert decomp._task_requires_independent_qa(task) is False


@pytest.mark.parametrize(
    "title",
    [
        "Assign an independent reviewer",
        "Have it independently reviewed",
        "Review by a different assignee",
        "Review_by_a_different_assignee",
        "Reviewed by someone else",
    ],
)
def test_explicit_independent_review_phrasings_reject_direct_promotion(
    kanban_home, title
):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title=title, triage=True)

    payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "incorrect direct promotion",
        "title": title,
        "body": "Implement and verify it.",
        "assignee": "engineer",
    })
    patches = _patch_list_profiles(["orchestrator", "engineer", "reviewer"])
    for patcher in patches:
        patcher.start()
    try:
        with _patch_aux_client(payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for patcher in patches:
            patcher.stop()

    assert outcome.ok is False
    assert "independent QA requires implementation and reviewer cards" in outcome.reason
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "triage"


def test_decompose_rejects_same_assignee_for_explicit_independent_qa(
    kanban_home,
):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Implement parser with independent QA",
            body="A separate reviewer must perform independent verification.",
            triage=True,
        )

    payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "implementation followed by independent QA",
        "coordination_reasons": ["independent_qa"],
        "tasks": [
            {
                "title": "implement parser",
                "body": "build it",
                "assignee": "engineer",
                "parents": [],
                "role": "implementation",
            },
            {
                "title": "independent QA",
                "body": "verify it independently",
                "assignee": "engineer",
                "parents": [0],
                "role": "independent_qa",
            },
        ],
    })
    patches = _patch_list_profiles(["orchestrator", "engineer", "reviewer"])
    for patcher in patches:
        patcher.start()
    try:
        with _patch_aux_client(payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for patcher in patches:
            patcher.stop()

    assert outcome.ok is False
    assert "independent QA" in outcome.reason
    assert "distinct assignee" in outcome.reason
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "triage"


def test_decompose_rejects_specialist_as_implementation_for_independent_qa(
    kanban_home,
):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Parser implementation with independent QA",
            triage=True,
        )

    payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "research followed by QA",
        "coordination_reasons": ["independent_qa"],
        "tasks": [
            {
                "title": "research parser options",
                "body": "research only",
                "assignee": "engineer",
                "parents": [],
                "role": "specialist",
            },
            {
                "title": "independent QA",
                "body": "verify independently",
                "assignee": "reviewer",
                "parents": [0],
                "role": "independent_qa",
            },
        ],
    })
    patches = _patch_list_profiles(["orchestrator", "engineer", "reviewer"])
    for patcher in patches:
        patcher.start()
    try:
        with _patch_aux_client(payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for patcher in patches:
            patcher.stop()

    assert outcome.ok is False
    assert "implementation and independent_qa roles" in outcome.reason
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "triage"


def test_decompose_accepts_distinct_explicit_independent_qa(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Implement parser with independent QA",
            body="A separate reviewer must perform independent verification.",
            triage=True,
        )

    payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "implementation followed by independent QA",
        "coordination_reasons": ["independent_qa"],
        "tasks": [
            {
                "title": "implement parser",
                "body": "build it",
                "assignee": "engineer",
                "parents": [],
                "role": "implementation",
            },
            {
                "title": "independent QA",
                "body": "verify it independently",
                "assignee": "reviewer",
                "parents": [0],
                "role": "independent_qa",
            },
        ],
    })
    patches = _patch_list_profiles(["orchestrator", "engineer", "reviewer"])
    for patcher in patches:
        patcher.start()
    try:
        with _patch_aux_client(payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for patcher in patches:
            patcher.stop()

    assert outcome.ok is True
    assert outcome.fanout is True
    assert outcome.child_ids and len(outcome.child_ids) == 2
    with kb.connect() as conn:
        implementation = kb.get_task(conn, outcome.child_ids[0])
        qa = kb.get_task(conn, outcome.child_ids[1])
    assert implementation.assignee == "engineer"
    assert qa.assignee == "reviewer"


def test_independent_qa_graph_must_cover_every_implementation_card(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Implement two components with independent QA",
            triage=True,
        )

    payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "parallel implementation followed by independent QA",
        "coordination_reasons": ["independent_qa"],
        "tasks": [
            {
                "title": "implement parser",
                "body": "build parser",
                "assignee": "engineer-a",
                "parents": [],
                "role": "implementation",
            },
            {
                "title": "implement serializer",
                "body": "build serializer",
                "assignee": "engineer-b",
                "parents": [],
                "role": "implementation",
            },
            {
                "title": "independent QA",
                "body": "verify both artifacts",
                "assignee": "reviewer",
                "parents": [0],
                "role": "independent_qa",
            },
        ],
    })
    patches = _patch_list_profiles(
        ["orchestrator", "engineer-a", "engineer-b", "reviewer"]
    )
    for patcher in patches:
        patcher.start()
    try:
        with _patch_aux_client(payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for patcher in patches:
            patcher.stop()

    assert outcome.ok is False
    assert "cover every implementation card" in outcome.reason
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "triage"


def test_independent_qa_may_cover_implementations_through_integration_card(
    kanban_home,
):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Integrate two components then assign an independent reviewer",
            triage=True,
        )

    payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "integrated implementation followed by independent QA",
        "coordination_reasons": ["independent_qa"],
        "tasks": [
            {
                "title": "implement parser",
                "body": "build parser",
                "assignee": "engineer-a",
                "parents": [],
                "role": "implementation",
            },
            {
                "title": "implement serializer",
                "body": "build serializer",
                "assignee": "engineer-b",
                "parents": [],
                "role": "implementation",
            },
            {
                "title": "integrate components",
                "body": "combine both implementation artifacts",
                "assignee": "integrator",
                "parents": [0, 1],
                "role": "implementation",
            },
            {
                "title": "independent QA",
                "body": "verify the integrated artifact",
                "assignee": "reviewer",
                "parents": [2],
                "role": "independent_qa",
            },
        ],
    })
    patches = _patch_list_profiles(
        ["orchestrator", "engineer-a", "engineer-b", "integrator", "reviewer"]
    )
    for patcher in patches:
        patcher.start()
    try:
        with _patch_aux_client(payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for patcher in patches:
            patcher.stop()

    assert outcome.ok is True, outcome.reason
    assert outcome.child_ids and len(outcome.child_ids) == 4


def test_decompose_fanout_false_preserves_existing_assignee(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="already routed",
            assignee="engineer",
            triage=True,
        )

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Keep existing lane.",
        "assignee": "fallback",
    })

    patches = _patch_list_profiles(["orchestrator", "engineer", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "engineer"
    assert task.title == "Tightened title"


def test_decompose_fanout_false_uses_valid_llm_assignee(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="route me", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Route to specialist.",
        "assignee": "engineer",
    })

    patches = _patch_list_profiles(["orchestrator", "engineer", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "engineer"


def test_decompose_fanout_false_invalid_llm_assignee_uses_default(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="route me safely", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Route to fallback.",
        "assignee": "made_up",
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "fallback"


def test_decompose_unknown_assignee_falls_back_to_default(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)

    # Roster only has 'orchestrator' and 'fallback'; LLM picks 'made_up'.
    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "cross-profile handoff",
        "coordination_reasons": ["durable_handoff"],
        "tasks": [
            {
                "title": "do X",
                "body": "",
                "assignee": "made_up",
                "parents": [],
                "role": "implementation",
            },
            {
                "title": "own final result",
                "body": "",
                "assignee": "orchestrator",
                "parents": [0],
                "role": "final_owner",
            },
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with patch.dict(
            "os.environ", {}, clear=False,
        ), _patch_aux_client(llm_payload), _patch_extra_body(), \
            patch(
                "hermes_cli.kanban_decompose._load_config",
                return_value={
                    "kanban": {
                        "orchestrator_profile": "orchestrator",
                        "default_assignee": "fallback",
                    }
                },
            ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.child_ids and len(outcome.child_ids) == 2
    with kb.connect() as conn:
        child = kb.get_task(conn, outcome.child_ids[0])
    # 'made_up' wasn't in roster, so assignee rewritten to 'fallback'
    assert child.assignee == "fallback"


@pytest.mark.parametrize("boolean_parent", [False, True])
def test_decompose_rejects_boolean_parent_indices(
    kanban_home, boolean_parent,
):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="reject boolean parent", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "cross-profile handoff",
        "coordination_reasons": ["durable_handoff"],
        "tasks": [
            {
                "title": "research",
                "assignee": "researcher",
                "parents": [],
                "role": "implementation",
            },
            {
                "title": "build",
                "assignee": "engineer",
                "parents": [],
                "role": "implementation",
            },
            {
                "title": "final",
                "assignee": "orchestrator",
                "parents": [boolean_parent],
                "role": "final_owner",
            },
        ],
    })

    patches = _patch_list_profiles(
        ["orchestrator", "researcher", "engineer"],
    )
    for patcher in patches:
        patcher.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for patcher in patches:
            patcher.stop()

    assert outcome.ok is False
    assert "parent index must be an integer" in outcome.reason
    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
    assert root is not None
    assert root.status == "triage"


def test_decompose_handles_malformed_llm_json(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client("not json at all, sorry"), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    assert "malformed JSON" in outcome.reason


def test_decompose_returns_false_when_task_not_triage(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x")  # ready, not triage

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()
    assert outcome.ok is False
    assert "not in triage" in outcome.reason


def test_decompose_no_aux_client_configured(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        # call_llm raises RuntimeError when no provider is configured; the
        # decomposer must convert that into a failed outcome, not a crash.
        with patch(
            "agent.auxiliary_client.call_llm",
            side_effect=RuntimeError("No LLM provider configured"),
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    # call_llm's no-provider RuntimeError surfaces via the LLM-error branch.
    assert "LLM error" in outcome.reason
