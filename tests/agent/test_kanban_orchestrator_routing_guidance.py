"""Prompt-contract tests for named-team Kanban orchestrator routing."""

from agent import prompt_builder


def test_configured_orchestrator_gets_role_boundary_preflight():
    guidance = prompt_builder.select_kanban_session_guidance(
        enabled_toolsets=["kanban"],
        valid_tool_names={"kanban_show", "kanban_create", "delegate_task"},
        task_id=None,
    )

    assert "Kanban orchestrator routing preflight" in guidance
    assert "direct explanation" in guidance
    assert "repository mutation" in guidance
    assert "sourced research" in guidance
    assert "independent verdict" in guidance
    assert "review" in guidance
    assert "generic `delegate_task` leaf" in guidance
    assert "You own one board task" not in guidance


def test_configured_orchestrator_fails_closed_without_named_routing_tool():
    guidance = prompt_builder.select_kanban_session_guidance(
        enabled_toolsets=["kanban"],
        valid_tool_names={"delegate_task"},
        task_id=None,
    )

    assert "`kanban_create`" in guidance
    assert "Do not silently substitute" in guidance
    assert "capability blocker" in guidance


def test_worker_keeps_worker_protocol_instead_of_orchestrator_preflight():
    guidance = prompt_builder.select_kanban_session_guidance(
        enabled_toolsets=["kanban"],
        valid_tool_names={"kanban_show", "kanban_create"},
        task_id="t_worker",
    )

    assert "Kanban worker protocol" in guidance
    assert "Kanban orchestrator routing preflight" not in guidance


def test_normal_chat_without_kanban_has_no_routing_preflight():
    guidance = prompt_builder.select_kanban_session_guidance(
        enabled_toolsets=None,
        valid_tool_names={"delegate_task"},
        task_id=None,
    )

    assert guidance == ""


def test_agent_init_installs_orchestrator_preflight(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("toolsets:\n  - kanban\n")
    monkeypatch.setenv("HERMES_HOME", str(home))

    from model_tools import _clear_tool_defs_cache
    from run_agent import AIAgent
    from tools.registry import invalidate_check_fn_cache

    invalidate_check_fn_cache()
    _clear_tool_defs_cache()
    agent = AIAgent(
        api_key="test",
        base_url="https://openrouter.ai/api/v1",
        enabled_toolsets=["kanban"],
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    assert "kanban_create" in agent.valid_tool_names
    assert "Kanban orchestrator routing preflight" in agent._kanban_worker_guidance
    assert "Kanban worker protocol" not in agent._kanban_worker_guidance
