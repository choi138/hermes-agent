"""Tests for kb.decompose_triage_task — the DB-layer atomic fan-out
from the triage column. LLM-free by design.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_triage(conn, title="rough idea", body=None, assignee=None, tenant=None):
    return kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        tenant=tenant,
        triage=True,
    )


def test_decompose_creates_children_and_promotes_root(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="ship a feature")
        assert kb.get_task(conn, tid).status == "triage"

    children = [
        {"title": "research", "body": "look at prior art", "assignee": "researcher", "parents": []},
        {"title": "build it", "body": "write code", "assignee": "engineer", "parents": [0]},
    ]
    with kb.connect() as conn:
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=children,
            author="decomposer",
        )
    assert child_ids is not None
    assert len(child_ids) == 2

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, child_ids[0])
        c1 = kb.get_task(conn, child_ids[1])

    # Root flipped to todo with orchestrator assignee, gated by children.
    assert root.status == "todo"
    assert root.assignee == "orchestrator"
    # First child has no internal parents → ready on recompute_ready.
    assert c0.status == "ready"
    assert c0.assignee == "researcher"
    # Second child has parents=[0] → stays in todo until c0 completes.
    assert c1.status == "todo"
    assert c1.assignee == "engineer"


def test_decompose_records_audit_comment_and_event(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[{
                "title": "task A",
                "assignee": "researcher",
                "role": "independent_qa",
            }],
            author="alice",
        )
    assert child_ids is not None

    with kb.connect() as conn:
        comments = kb.list_comments(conn, tid)
        events = kb.list_events(conn, tid)
        child_events = kb.list_events(conn, child_ids[0])

    assert any("Decomposed into" in (c.body or "") for c in comments)
    assert any(ev.kind == "decomposed" for ev in events)
    created = next(ev for ev in child_events if ev.kind == "created")
    assert created.payload["role"] == "independent_qa"


def test_decompose_allows_more_than_three_cards_within_three_stages(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[
                {"title": "specialist A", "parents": []},
                {"title": "specialist B", "parents": []},
                {"title": "independent QA", "parents": [0, 1]},
                {"title": "final owner", "parents": [2]},
            ],
            author="me",
        )

    assert child_ids is not None
    assert len(child_ids) == 4


def test_decompose_rejects_more_than_three_initial_stages(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        with pytest.raises(ValueError, match="at most 3 stages"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orch",
                children=[
                    {"title": "stage 1", "parents": []},
                    {"title": "stage 2", "parents": [0]},
                    {"title": "stage 3", "parents": [1]},
                    {"title": "stage 4", "parents": [2]},
                ],
                author="me",
            )


def test_decompose_children_default_out_of_root_goal_mode(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="open-ended root",
            assignee="worker",
            triage=True,
            goal_mode=True,
            goal_max_turns=4,
        )
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=[{"title": "bounded deterministic child"}],
            author="decomposer",
        )
        child = kb.get_task(conn, child_ids[0])

    assert child.goal_mode is False
    assert child.goal_max_turns is None
