from __future__ import annotations

from types import SimpleNamespace

from hermes_state import SessionDB


def test_compression_split_republishes_runtime_state_for_new_session(
    monkeypatch, tmp_path
):
    from agent.conversation_compression import compress_context

    events = []

    def fake_invoke_hook(hook_name, **kwargs):
        events.append((hook_name, kwargs))
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", fake_invoke_hook)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    class Compressor:
        compression_count = 1
        _last_compress_aborted = False
        _last_summary_error = None
        _last_aux_model_failure_model = None
        _last_aux_model_failure_error = None
        _last_compression_made_progress = True
        _last_summary_fallback_used = False
        _last_feasibility_skip = False
        last_compression_rough_tokens = 0
        last_prompt_tokens = 0
        last_completion_tokens = 0
        awaiting_real_usage_after_compression = False

        def compress(self, messages, **kwargs):
            return [{"role": "user", "content": "[summary]"}]

    session_db = SessionDB(db_path=tmp_path / "state.db")
    session_db.create_session(
        session_id="old-session",
        source="discord",
        model="gpt-5.5",
    )
    agent = SimpleNamespace(
        _compression_feasibility_checked=True,
        compression_in_place=False,
        session_id="old-session",
        model="gpt-5.5",
        provider="codex-nekos",
        base_url="https://codex.nekos.me/v1",
        api_key="do-not-publish",
        api_mode="codex_responses",
        reasoning_config={"enabled": True, "effort": "high"},
        platform="discord",
        _emit_status=lambda *a, **k: None,
        _emit_warning=lambda *a, **k: None,
        _memory_manager=None,
        context_compressor=Compressor(),
        _todo_store=SimpleNamespace(format_for_injection=lambda: ""),
        _invalidate_system_prompt=lambda: None,
        _build_system_prompt=lambda system_message: "new-system",
        _cached_system_prompt=None,
        _session_db=session_db,
        _session_init_model_config={},
        _session_db_created=True,
        _last_flushed_db_idx=0,
        _flush_messages_to_session_db=lambda *a, **k: None,
        _vprint=lambda *a, **k: None,
        log_prefix="",
        tools=None,
    )
    agent.commit_memory_session = lambda messages: None

    try:
        compressed, new_prompt = compress_context(
            agent,
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "world"},
            ],
            "system",
            task_id="old-session",
        )

        assert compressed == [{"role": "user", "content": "[summary]"}]
        assert new_prompt == "new-system"
        assert agent.session_id != "old-session"
        assert session_db.get_session(agent.session_id)["parent_session_id"] == (
            "old-session"
        )

        runtime_events = [kwargs for hook, kwargs in events if hook == "runtime_state"]
        assert runtime_events, (
            "compression split should emit runtime_state before later tool gates fire"
        )
        event = runtime_events[0]
        assert event["session_id"] == agent.session_id
        assert event["task_id"] == "old-session"
        assert event["state"] == {
            "session_id": agent.session_id,
            "task_id": "old-session",
            "model": "gpt-5.5",
            "provider": "codex-nekos",
            "base_url": "https://codex.nekos.me/v1",
            "api_mode": "codex_responses",
            "platform": "discord",
            "reasoning_effort": "high",
            "parent_session_id": "old-session",
            "boundary_reason": "compression",
        }
        assert "api_key" not in event["state"]
    finally:
        session_db.close()
