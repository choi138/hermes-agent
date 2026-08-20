"""Tests for kanban goal_mode — per-card Ralph-style goal loop.

Covers three layers:

1. DB: goal_mode / goal_max_turns persist through create_task + from_row,
   and a legacy DB (without the columns) migrates cleanly.
2. Spawn: _default_spawn sets the HERMES_KANBAN_GOAL_MODE env vars only
   when the card opts in.
3. Loop: goals.run_kanban_goal_loop continuation / completion / budget
   behaviour, driven entirely through injected callbacks (no live model).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import goals


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------





def test_legacy_db_migrates_goal_columns(tmp_path, monkeypatch):
    """A tasks table created without goal columns must gain them on init."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    db_path = kb.kanban_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Minimal legacy schema: tasks table missing goal_mode / goal_max_turns.
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL DEFAULT 'ready',
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
        """
    )
    legacy.execute(
        "INSERT INTO tasks (id, title, status, priority, created_at, workspace_kind) "
        "VALUES ('legacy1', 'old', 'ready', 0, 1, 'scratch')"
    )
    legacy.commit()
    legacy.close()

    # init_db runs the additive migration.
    kb.init_db()
    with kb.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "goal_mode" in cols
        assert "goal_max_turns" in cols
        task = kb.get_task(conn, "legacy1")
    # Existing row keeps the safe default.
    assert task.goal_mode is False
    assert task.goal_max_turns is None


# ---------------------------------------------------------------------------
# Spawn env
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Goal loop logic (callback-injected, no live model)
# ---------------------------------------------------------------------------

def _patch_judge(monkeypatch, verdicts):
    """Make judge_goal return a scripted sequence of verdicts."""
    seq = list(verdicts)

    def _fake_judge(goal, response, subgoals=None, background_processes=None, **_kw):
        v = seq.pop(0) if seq else "done"
        # 5-tuple contract: verdict, reason, parse failure, wait, transport failure.
        return v, f"scripted:{v}", False, None, False

    monkeypatch.setattr(goals, "judge_goal", _fake_judge)


def test_loop_stops_when_worker_already_completed(monkeypatch):
    # Worker called kanban_complete on its first turn — no judging needed.
    _patch_judge(monkeypatch, ["continue"])  # should never be consulted
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="t1",
        goal_text="do the thing",
        run_turn=lambda p: turns.append(p) or "x",
        task_status_fn=lambda: "done",
        block_fn=lambda r: pytest.fail("should not block"),
        first_response="done already",
    )
    assert res["outcome"] == "completed_by_worker"
    assert turns == []  # no extra turns






def test_cli_goal_loop_shares_iteration_budget_and_emits_progress(
    kanban_home,
    monkeypatch,
):
    import cli as cli_module

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="observable bounded worker",
            assignee="default",
            goal_mode=True,
            goal_max_turns=goals.KANBAN_DEFAULT_MAX_TURNS,
        )
        kb.claim_task(conn, tid)
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.current_run_id is not None
    assert task.claim_lock
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", task.claim_lock)
    monkeypatch.setenv("HERMES_LANGUAGE", "ko")

    class _FakeAgent:
        max_iterations = 5
        session_id = "session-1"

        def __init__(self):
            self.clamps = []

        def run_conversation(self, *, user_message, conversation_history):
            self.clamps.append(self.max_iterations)
            used = 2 if len(self.clamps) == 1 else 1
            return {
                "final_response": f"continued:{user_message[:8]}",
                "api_calls": used,
            }

    agent = _FakeAgent()

    class _FakeCLI:
        conversation_history = []
        session_id = "session-1"

        def __init__(self):
            self.agent = agent

    captured = {}

    def _fake_loop(**kwargs):
        captured.update(kwargs)
        assert kwargs["max_turns"] == goals.KANBAN_DEFAULT_MAX_TURNS
        assert kwargs["iteration_budget_fn"]() == (2, 5)
        kwargs["progress_fn"]({
            "stage": "judge",
            "turn": 1,
            "max_turns": goals.KANBAN_DEFAULT_MAX_TURNS,
            "verdict": "continue",
            "reason": "run the deterministic checks",
            "iterations_used": 2,
            "iterations_total": 5,
        })
        # Same semantic verdict inside the three-turn window is kept in logs
        # but does not multiply chat notifications.
        kwargs["progress_fn"]({
            "stage": "judge",
            "turn": 2,
            "max_turns": goals.KANBAN_DEFAULT_MAX_TURNS,
            "verdict": "continue",
            "reason": "this intermediate note is throttled",
            "iterations_used": 3,
            "iterations_total": 5,
        })
        kwargs["progress_fn"]({
            "stage": "judge",
            "turn": 4,
            "max_turns": goals.KANBAN_DEFAULT_MAX_TURNS,
            "verdict": "continue",
            "reason": "report the periodic milestone",
            "iterations_used": 4,
            "iterations_total": 5,
        })
        kwargs["run_turn"]("continue one")
        assert kwargs["iteration_budget_fn"]() == (4, 5)
        kwargs["run_turn"]("continue two")
        assert kwargs["iteration_budget_fn"]() == (5, 5)
        return {"outcome": "blocked_iterations"}

    monkeypatch.setattr(goals, "run_kanban_goal_loop", _fake_loop)

    cli_module._run_kanban_goal_loop_q(
        _FakeCLI(),
        first_response="initial",
        first_api_calls=2,
    )

    assert agent.clamps == [3, 1]
    assert agent.max_iterations == 5
    assert captured["first_response"] == "initial"

    with kb.connect() as conn:
        heartbeats = [
            event
            for event in kb.list_events(conn, tid)
            if event.kind == "heartbeat" and (event.payload or {}).get("note")
        ]
    assert len(heartbeats) == 2
    notes = [heartbeat.payload["note"] for heartbeat in heartbeats]
    assert "**현재 단계:** 작업 완료 조건을 검토하고 있습니다" in notes[0]
    assert "**확인:** 완료 조건을 아직 충족하지 못했습니다" in notes[0]
    assert "**다음:** run the deterministic checks" in notes[0]
    assert "report the periodic milestone" in notes[1]
    assert all("this intermediate note is throttled" not in note for note in notes)
    for forbidden in (
        "Goal step",
        "Judge:",
        "Primary calls",
        "iteration ",
        "terminal",
        "**Current stage:**",
        "**Confirmed:**",
        "**Next:**",
    ):
        assert all(forbidden not in note for note in notes)


# ---------------------------------------------------------------------------
# CLI judge gate tests (hermes kanban complete bypass fix)
# ---------------------------------------------------------------------------

class TestCLIJudgeGate:
    """hermes kanban complete must apply the same goal_mode judge gate as the
    kanban_complete tool (Issue #38367 sibling gap).

    Uses mocks for kb.get_task and kb.complete_task to avoid depending on the
    full kanban_db schema; the gate logic is the unit under test.
    """

    def _run(self, monkeypatch, *, goal_mode=True, judge_available=True,
             verdict="done", reason="", complete_ok=True, summary="done"):
        import argparse
        import types
        from unittest.mock import MagicMock
        from hermes_cli.kanban import _cmd_complete

        fake_task = types.SimpleNamespace(
            goal_mode=goal_mode,
            title="Finish report",
            body="acceptance: criteria",
        )
        fake_conn = MagicMock()
        complete_calls: list = []

        def fake_connect_closing():
            from contextlib import contextmanager
            @contextmanager
            def _cm():
                yield fake_conn
            return _cm()

        def fake_complete_task(conn, tid, **kw):
            complete_calls.append(tid)
            return complete_ok

        monkeypatch.setattr("hermes_cli.kanban.kb.get_task", lambda conn, tid: fake_task)
        monkeypatch.setattr("hermes_cli.kanban.kb.complete_task", fake_complete_task)
        monkeypatch.setattr("hermes_cli.kanban.kb.connect_closing", fake_connect_closing)
        monkeypatch.setattr("hermes_cli.kanban._worker_run_id_for", lambda _: None)

        _aux_client = (object(), "judge-model") if judge_available else (None, None)
        monkeypatch.setattr(
            "agent.auxiliary_client.get_text_auxiliary_client",
            lambda name: _aux_client,
        )
        # Match the real judge_goal contract:
        # (verdict, reason, parse_failed, wait_directive, transport_failed)
        monkeypatch.setattr(
            "hermes_cli.goals.judge_goal",
            lambda **kw: (verdict, reason, False, None, False),
        )

        args = argparse.Namespace(task_ids=["t1"], summary=summary, result=None, metadata=None)
        return _cmd_complete(args), complete_calls

    def test_judge_rejects_premature_completion(self, monkeypatch):
        rc, complete_calls = self._run(
            monkeypatch, verdict="continue", reason="criteria not met"
        )
        assert rc != 0, "judge rejection must produce non-zero exit code"
        assert complete_calls == [], (
            "complete_task must NOT be invoked when the judge rejects"
        )


    def test_non_goal_mode_task_skips_gate(self, monkeypatch):
        """Plain (non-goal_mode) tasks are never sent to the judge."""
        rc, complete_calls = self._run(monkeypatch, goal_mode=False)
        assert rc == 0
        assert complete_calls == ["t1"]


def test_goal_mode_requires_explicit_positive_max_turns(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="goal_max_turns must be >= 1"):
            kb.create_task(conn, title="t", assignee="worker", goal_mode=True)


def test_loop_default_budget_preserves_full_quality_boundary(monkeypatch):
    _patch_judge(monkeypatch, ["continue"] * 25)
    turns = []
    blocked = {}

    res = goals.run_kanban_goal_loop(
        task_id="t-default-budget",
        goal_text="bounded task",
        run_turn=lambda p: turns.append(p) or "still going",
        task_status_fn=lambda: "running",
        block_fn=lambda reason: blocked.update(reason=reason),
        first_response="turn1",
    )

    assert res["outcome"] == "blocked_budget"
    assert (
        res["turns_used"]
        == goals.KANBAN_DEFAULT_MAX_TURNS
        == goals.DEFAULT_MAX_TURNS
        == 20
    )
    assert len(turns) == 19
    assert "20/20" in blocked["reason"]
