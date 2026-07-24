import pytest

from gateway.session_context import _UNSET, _VAR_MAP, clear_session_vars, set_session_vars
from run_agent import AIAgent, _session_source_for_agent


@pytest.fixture(autouse=True)
def _reset_contextvars():
    for var in _VAR_MAP.values():
        var.set(_UNSET)
    yield
    for var in _VAR_MAP.values():
        var.set(_UNSET)


def test_session_source_context_overrides_platform(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)

    tokens = set_session_vars(source="tool")
    try:
        assert _session_source_for_agent("tui") == "tool"
    finally:
        clear_session_vars(tokens)


def test_session_source_falls_back_to_platform(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)

    assert _session_source_for_agent("tui") == "tui"


def test_session_source_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "webhook")

    assert _session_source_for_agent(None) == "webhook"


def test_kanban_worker_session_persists_distinct_source_and_workspace(
    monkeypatch, tmp_path
):
    """Worker usage is separable from direct CLI usage without losing cwd."""
    from hermes_state import SessionDB

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_metrics_boundary")
    monkeypatch.setenv("TERMINAL_ENV", "local")

    db = SessionDB(db_path=tmp_path / "state.db")
    agent = AIAgent.__new__(AIAgent)
    agent._persist_disabled = False
    agent._session_db_created = False
    agent._session_db = db
    agent.platform = "cli"
    agent.session_id = "kanban-metrics-session"
    agent.model = "test/model"
    agent._session_init_model_config = {}
    agent._cached_system_prompt = "stable system prompt"
    agent._parent_session_id = None

    agent._ensure_db_session()

    row = db.get_session(agent.session_id)
    assert row["source"] == "kanban"
    assert row["cwd"] == str(workspace)
