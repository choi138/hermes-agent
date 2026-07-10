from pathlib import Path
from types import SimpleNamespace


def _config(**overrides):
    config = {
        "smart_model_routing": {
            "enabled": True,
            "respect_explicit_model": False,
            "gates": {
                "repo_mutation": "block",
                "high_risk": "allow",
            },
            "routes": {
                "cheap_chat": {"model": "cheap-model"},
                "reasoning": {"provider": "openrouter", "model": "reasoning-model"},
                "codex_implementation": {"provider": "openrouter", "model": "codex-model"},
                "research_readonly": {"provider": "openrouter", "model": "research-model"},
                "multimodal": {"provider": "openrouter", "model": "vision-model"},
            },
        }
    }
    config["smart_model_routing"].update(overrides)
    return config


def test_disabled_preserves_primary_route():
    from hermes_cli.smart_model_routing import decide_route

    decision = decide_route(
        "what time is it?",
        config={"smart_model_routing": {"enabled": False}},
    )

    assert decision.enabled is False
    assert decision.selected_lane == "primary"
    assert decision.should_route is False


def test_simple_readonly_routes_to_cheap_lane():
    from hermes_cli.smart_model_routing import decide_route

    decision = decide_route("what is sqlite?", config=_config())

    assert decision.enabled is True
    assert decision.selected_lane == "cheap_chat"
    assert decision.target.model == "cheap-model"
    assert decision.fail_closed is False


def test_freshness_routes_to_research_lane():
    from hermes_cli.smart_model_routing import decide_route

    decision = decide_route("search the latest release notes with citations", config=_config())

    assert decision.selected_lane == "research_readonly"
    assert decision.target.model == "research-model"


def test_multimodal_hint_routes_to_multimodal_lane():
    from hermes_cli.smart_model_routing import decide_route

    decision = decide_route("describe this screenshot", config=_config(), has_multimodal=True)

    assert decision.selected_lane == "multimodal"
    assert decision.target.model == "vision-model"


def test_repo_mutation_fails_closed_before_model_selection():
    from hermes_cli.smart_model_routing import decide_route

    decision = decide_route("implement the auth migration", config=_config())

    assert decision.fail_closed is True
    assert decision.selected_lane == "blocked"
    assert decision.blockers == ("repo_mutation_requires_coordination",)
    assert decision.should_route is False


def test_ordinary_coding_routes_to_codex_default_when_mutation_gate_allows():
    from hermes_cli.smart_model_routing import decide_route

    cfg = _config(gates={"repo_mutation": "allow", "high_risk": "allow"})

    decision = decide_route("fix the parser bug and add a regression test", config=cfg)

    assert decision.selected_lane == "codex_implementation"
    assert decision.target.model == "codex-model"
    assert decision.allow_mutation is True
    assert "repo_write" in decision.mutation_classes


def test_readonly_cleanup_candidates_do_not_count_as_delete_execution():
    from hermes_cli.smart_model_routing import decide_route

    decision = decide_route(
        (
            "현재 내 컴퓨터에 저장공간이 다 차서 필요없는 캐시 같은것들 지워서 "
            "저장공간 확보해야할것 같아. 일단 필요없고 삭제해도 되는게 "
            "어던것들인지 listup해서 알려줘"
        ),
        config=_config(),
    )

    assert decision.fail_closed is False
    assert decision.selected_lane == "reasoning"
    assert decision.classification.readonly_cleanup_advice is True
    assert decision.classification.repo_mutation is False
    assert decision.classification.high_risk is False
    assert decision.mutation_classes == ()


def test_simple_delete_routes_to_codex_not_gjc_when_gates_allow():
    from hermes_cli.smart_model_routing import decide_route

    decision = decide_route(
        "필요없는 캐시 파일 삭제해줘",
        config=_config(gates={"repo_mutation": "allow", "high_risk": "allow"}),
    )

    assert decision.fail_closed is False
    assert decision.selected_lane == "codex_implementation"
    assert decision.target.model == "codex-model"
    assert decision.classification.high_risk is True
    assert "destructive" in decision.mutation_classes


def test_high_risk_can_still_block_when_gate_is_explicitly_configured():
    from hermes_cli.smart_model_routing import decide_route

    decision = decide_route(
        "delete the auth migration",
        config=_config(gates={"repo_mutation": "allow", "high_risk": "block"}),
    )

    assert decision.fail_closed is True
    assert decision.selected_lane == "blocked"
    assert decision.blockers == ("high_risk_requires_coordination",)


def test_explicit_gjc_request_fails_closed_until_coordinator_exists():
    from hermes_cli.smart_model_routing import decide_route

    decision = decide_route("use GJC ralplan for this architecture proposal", config=_config())

    assert decision.selected_lane == "gjc_ralplan"
    assert decision.fail_closed is True
    assert decision.blockers == ("gjc_coordination_required",)
    assert "coordinator_mcp" in decision.required_gates


def test_large_port_routes_to_gjc_ralplan_gate():
    from hermes_cli.smart_model_routing import decide_route

    decision = decide_route(
        "port the payments package from repo-a to repo-b",
        config=_config(gates={"repo_mutation": "allow", "high_risk": "allow"}),
    )

    assert decision.selected_lane == "gjc_ralplan"
    assert decision.fail_closed is True
    assert decision.classification.large_migration_or_port is True


def test_gjc_gate_allow_defers_to_durable_coordination():
    from hermes_cli.smart_model_routing import decide_route

    cfg = _config(
        gates={"repo_mutation": "allow", "high_risk": "allow", "gjc_escalation": "allow"},
        gjc_coordinator={"enabled": True, "command": "gjc", "tool": "start_workflow"},
    )

    decision = decide_route("use GJC ralplan for this architecture proposal", config=cfg)

    assert decision.selected_lane == "gjc_ralplan"
    assert decision.fail_closed is False
    assert decision.gjc_workflow == "ralplan"
    assert "coordinator_mcp" in decision.required_gates


def test_provider_target_resolution_is_best_effort(monkeypatch):
    from hermes_cli.smart_model_routing import apply_decision_to_runtime, decide_route

    def fake_resolve_runtime_provider(**kwargs):
        assert kwargs["requested"] == "openrouter"
        assert kwargs["target_model"] == "reasoning-model"
        return {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-routed",
            "source": "test",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        fake_resolve_runtime_provider,
    )

    decision = decide_route("plan the architecture", config=_config())
    model, runtime, metadata = apply_decision_to_runtime(
        decision,
        current_model="primary-model",
        current_runtime={
            "provider": "custom",
            "api_mode": "chat_completions",
            "base_url": "https://local.example/v1",
            "api_key": "sk-primary",
        },
    )

    assert model == "reasoning-model"
    assert runtime["provider"] == "openrouter"
    assert runtime["api_key"] == "sk-routed"
    assert metadata["applied"] is True


def test_cli_turn_route_applies_configured_model():
    from cli import HermesCLI

    shell = SimpleNamespace(
        model="primary-model",
        api_key="sk-primary",
        base_url="https://local.example/v1",
        provider="custom",
        api_mode="chat_completions",
        acp_command=None,
        acp_args=[],
        _credential_pool=None,
        config=_config(routes={"cheap_chat": {"model": "cheap-model"}}),
        _model_is_default=True,
        service_tier=None,
    )

    route = HermesCLI._resolve_turn_agent_config.__get__(shell)("what is sqlite?")

    assert route["model"] == "cheap-model"
    assert route["runtime"]["provider"] == "custom"
    assert route["smart_routing"]["selected_lane"] == "cheap_chat"


def test_cli_turn_route_returns_blocker_for_mutation():
    from cli import HermesCLI

    shell = SimpleNamespace(
        model="primary-model",
        api_key="sk-primary",
        base_url="https://local.example/v1",
        provider="custom",
        api_mode="chat_completions",
        acp_command=None,
        acp_args=[],
        _credential_pool=None,
        config=_config(),
        _model_is_default=True,
        service_tier=None,
    )

    route = HermesCLI._resolve_turn_agent_config.__get__(shell)("delete the auth migration")

    assert route["model"] == "primary-model"
    assert route["blocked"]["blockers"] == ["repo_mutation_requires_coordination"]


def test_gateway_turn_route_uses_same_policy():
    from gateway.run import GatewayRunner

    runner = SimpleNamespace(
        _service_tier=None,
        config=_config(routes={"multimodal": {"model": "vision-model"}}),
    )
    runtime_kwargs = {
        "api_key": "sk-primary",
        "base_url": "https://local.example/v1",
        "provider": "custom",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
    }

    route = GatewayRunner._resolve_turn_agent_config.__get__(runner)(
        "describe this screenshot",
        "primary-model",
        runtime_kwargs,
        attachment_count=1,
    )

    assert route["model"] == "vision-model"
    assert route["smart_routing"]["selected_lane"] == "multimodal"


def test_kanban_policy_decision_persists_with_event(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.smart_model_routing import decision_metadata, decide_route

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="policy persistence", assignee="alice")
        decision = decide_route(
            "fix the parser bug",
            config=_config(gates={"repo_mutation": "allow", "high_risk": "allow"}),
        )
        decision_id = kb.record_policy_decision(
            conn,
            task_id,
            decision_metadata(decision),
        )
        records = kb.list_policy_decisions(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert decision_id > 0
    assert len(records) == 1
    assert records[0].selected_lane == "codex_implementation"
    assert records[0].allow_mutation is True
    assert records[0].mutation_classes == ["repo_write"]
    assert records[0].classification["coding_work"] is True
    assert events[-1].kind == "policy_decision"
    assert events[-1].payload["policy_decision_id"] == decision_id


def test_gjc_route_requests_approval_before_execution(tmp_path, monkeypatch):
    from cli import HermesCLI
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="gjc approval", assignee="alice")

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    cfg = _config(
        gates={"repo_mutation": "allow", "high_risk": "allow", "gjc_escalation": "allow"},
        gjc_coordinator={"enabled": True, "command": "gjc", "tool": "start_workflow"},
    )
    shell = SimpleNamespace(
        model="primary-model",
        api_key="sk-primary",
        base_url="https://local.example/v1",
        provider="custom",
        api_mode="chat_completions",
        acp_command=None,
        acp_args=[],
        _credential_pool=None,
        config=cfg,
        _model_is_default=True,
        service_tier=None,
    )

    route = HermesCLI._resolve_turn_agent_config.__get__(shell)(
        "use GJC ralplan for this architecture proposal"
    )

    assert route["blocked"]["blockers"] == ["gjc_approval_required"]
    with kb.connect_closing() as conn:
        approvals = kb.list_task_approvals(conn, task_id)
    assert len(approvals) == 1
    assert approvals[0].status == "pending"
    assert approvals[0].approval_type == "gjc_escalation"


def test_gjc_route_prepares_execution_after_approval(tmp_path, monkeypatch):
    from cli import HermesCLI
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="gjc approved", assignee="alice")
        kb.approve_latest_task_approval(
            conn,
            task_id,
            approval_type="gjc_escalation",
            resolved_by="tester",
        )

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    cfg = _config(
        gates={"repo_mutation": "allow", "high_risk": "allow", "gjc_escalation": "allow"},
        gjc_coordinator={
            "enabled": True,
            "command": "gjc",
            "args": ["mcp-serve", "coordinator"],
            "tool": "start_workflow",
        },
    )
    shell = SimpleNamespace(
        model="primary-model",
        api_key="sk-primary",
        base_url="https://local.example/v1",
        provider="custom",
        api_mode="chat_completions",
        acp_command=None,
        acp_args=[],
        _credential_pool=None,
        config=cfg,
        _model_is_default=True,
        service_tier=None,
    )

    route = HermesCLI._resolve_turn_agent_config.__get__(shell)(
        "use GJC ralplan for this architecture proposal"
    )

    assert "blocked" not in route
    assert route["gjc_execution"]["task_id"] == task_id
    assert route["gjc_execution"]["run_id"] == 42
    assert route["gjc_execution"]["workflow"] == "ralplan"


def test_gjc_execution_records_questions_evidence_and_session(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.gjc_coordinator import run_gjc_execution

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="gjc execution", assignee="alice")

    def fake_call(_coord, payload):
        assert payload["task_id"] == task_id
        return {
            "gjc_session_id": "s-gjc",
            "gjc_turn_id": "turn-1",
            "turn_status": "waiting",
            "questions": [{"question": "Which package is target?", "answer_shape": "text"}],
            "evidence_paths": ["/tmp/evidence.txt"],
            "artifact_refs": ["artifact://plan"],
            "final_response": "Waiting for answer.",
        }

    monkeypatch.setattr("hermes_cli.gjc_coordinator._call_mcp_tool", fake_call)

    result = run_gjc_execution(
        {
            "enabled": True,
            "task_id": task_id,
            "run_id": None,
            "lane": "gjc_ralplan",
            "workflow": "ralplan",
            "prompt": "plan it",
            "cwd": str(tmp_path),
            "routing": {"selected_lane": "gjc_ralplan"},
            "coordinator": {"command": "gjc", "args": [], "tool": "start_workflow"},
        }
    )

    assert result["question_ids"]
    with kb.connect_closing() as conn:
        questions = kb.list_open_task_questions(conn, task_id)
        evidence = kb.list_task_evidence(conn, task_id)
        sessions = kb.list_gjc_sessions(conn, task_id)
    assert questions[0].question == "Which package is target?"
    assert {item.kind for item in evidence} == {"gjc_artifact", "gjc_evidence"}
    assert sessions[-1].gjc_session_id == "s-gjc"
    assert sessions[-1].turn_status == "waiting"


def test_gjc_route_resumes_answered_questions_without_new_session(tmp_path, monkeypatch):
    from cli import HermesCLI
    from hermes_cli import kanban_db as kb
    from hermes_cli.gjc_coordinator import run_gjc_execution

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="gjc resume", assignee="alice")
        kb.approve_latest_task_approval(
            conn,
            task_id,
            approval_type="gjc_escalation",
            resolved_by="tester",
        )
        question_id = kb.request_task_question(
            conn,
            task_id,
            question="Which package is target?",
            metadata={"question_id": "q-ext"},
        )
        kb.answer_task_question(conn, question_id, answer="payments")
        gjc_record_id = kb.create_gjc_session(
            conn,
            task_id,
            lane="gjc_ralplan",
            workflow="ralplan",
            gjc_session_id="s-gjc",
            gjc_turn_id="turn-1",
            turn_status="waiting_for_answer",
            question_ids=[question_id],
            approval_gate="gjc_escalation",
        )

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    cfg = _config(
        gates={"repo_mutation": "allow", "high_risk": "allow", "gjc_escalation": "allow"},
        gjc_coordinator={
            "enabled": True,
            "command": "gjc",
            "tool": "start_workflow",
            "question_answer_tool": "submit_question_answer",
            "bounded_await_tool": "bounded_await",
        },
    )
    shell = SimpleNamespace(
        model="primary-model",
        api_key="sk-primary",
        base_url="https://local.example/v1",
        provider="custom",
        api_mode="chat_completions",
        acp_command=None,
        acp_args=[],
        _credential_pool=None,
        config=cfg,
        _model_is_default=True,
        service_tier=None,
    )

    route = HermesCLI._resolve_turn_agent_config.__get__(shell)(
        "use GJC ralplan for this architecture proposal"
    )

    assert "blocked" not in route
    assert route["gjc_execution"]["resume"]["gjc_record_id"] == gjc_record_id
    calls = []

    def fake_call(coord, payload):
        calls.append((coord["tool"], payload))
        if coord["tool"] == "submit_question_answer":
            assert payload["question_id"] == "q-ext"
            assert payload["answer"] == "payments"
            return {"status": "submitted"}
        assert coord["tool"] == "bounded_await"
        assert payload["gjc_session_id"] == "s-gjc"
        return {
            "status": "completed",
            "artifactRefs": ["artifact://done"],
            "evidencePaths": ["/tmp/done.txt"],
            "finalResponse": "Completed after answer.",
        }

    monkeypatch.setattr("hermes_cli.gjc_coordinator._call_mcp_tool", fake_call)

    result = run_gjc_execution(route["gjc_execution"])

    assert result["final_response"] == "Completed after answer."
    assert [tool for tool, _payload in calls] == ["submit_question_answer", "bounded_await"]
    with kb.connect_closing() as conn:
        sessions = kb.list_gjc_sessions(conn, task_id)
    assert len(sessions) == 1
    assert sessions[0].id == gjc_record_id
    assert sessions[0].turn_status == "completed"
