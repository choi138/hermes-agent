"""Regression tests for the ``_memory_ingest_disabled`` per-agent flag (ADR-004 Phase 0).

A forked AIAgent (background review, the future ingest curator) may share the
PARENT's ``_memory_manager`` for reads (``memory_search``) while guaranteeing
zero graph-write leakage. Before this flag existed there was no such toggle:
rebinding the manager onto a fork leaked the fork's harness prompt into the
user's real memory namespace through three ingestion sites — ``on_turn_start``
(cadence + turn message), ``prefetch_all`` (recall query), and ``sync_all``
(harness prompt + review output recorded as a (user, assistant) turn pair).

These tests instrument a stub provider and drive the REAL leak-site code:

* the turn prologue (``build_turn_context`` — on_turn_start + prefetch_all),
* the sync chokepoint (``AIAgent._sync_external_memory_for_turn`` — sync_all +
  queue_prefetch_all, shared by turn_finalizer and codex_runtime),
* the session boundary paths (``commit_memory_session`` /
  ``shutdown_memory_provider`` — on_session_end + shutdown_all),
* the background-review fork construction recipe,
* a full simulated ``run_conversation`` against an in-process mock provider
  (the CI-level "fork run produces zero ingest calls" guarantee — this is the
  test that catches a FUTURE hook re-opening a leak),

and assert that ``memory_search`` reads stay callable throughout. Each blocked
site has a flag-off control so the instrumentation itself is proven to detect
leaks (a vacuous zero-assert would pass with broken plumbing).

Port note: the prologue fake agent is imported from
``tests/agent/test_turn_context.py`` rather than duplicated. Upstream's copy
of ``_FakeAgent`` has drifted from our prologue (which now requires
``should_compress_info``, ``_warn_context_overflow_blocked``, and the persist
lock), and a stale duplicate would silently stop exercising the seam.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from agent.memory_manager import MemoryManager, memory_ingest_allowed
from agent.memory_provider import MemoryProvider
from agent.turn_context import build_turn_context

# Shared prologue harness — see the port note in the module docstring.
from tests.agent.test_turn_context import _FakeAgent


# ---------------------------------------------------------------------------
# Instrumented provider: counts every write-ish hook, serves memory_search
# ---------------------------------------------------------------------------

WRITE_HOOKS = (
    "sync_turn",
    "queue_prefetch",
    "prefetch",
    "on_turn_start",
    "on_session_end",
    "on_pre_compress",
    "on_session_switch",
    "on_delegation",
    "on_memory_write",
    "shutdown",
)


class _InstrumentedProvider(MemoryProvider):
    """Records every hook invocation; exposes a memory_search tool."""

    def __init__(self):
        self.calls: List[tuple] = []

    @property
    def name(self) -> str:
        return "instrumented"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str = "", **kwargs) -> None:
        pass

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [{
            "name": "memory_search",
            "description": "search memory",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        }]

    def handle_tool_call(self, tool_name, args, **kwargs) -> str:
        self.calls.append(("handle_tool_call", tool_name))
        return json.dumps({"success": True, "results": ["remembered fact"]})

    # -- write-ish hooks, all recorded --------------------------------------
    def prefetch(self, query, *, session_id: str = "") -> str:
        self.calls.append(("prefetch", query))
        return ""

    def queue_prefetch(self, query, *, session_id: str = "") -> None:
        self.calls.append(("queue_prefetch", query))

    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None) -> None:
        self.calls.append(("sync_turn", user_content))

    def on_turn_start(self, turn_number, message, **kwargs) -> None:
        self.calls.append(("on_turn_start", message))

    def on_session_end(self, messages) -> None:
        self.calls.append(("on_session_end", len(messages or [])))

    def on_session_switch(self, new_session_id, **kwargs) -> None:
        self.calls.append(("on_session_switch", new_session_id))

    def on_pre_compress(self, messages) -> str:
        self.calls.append(("on_pre_compress", len(messages or [])))
        return ""

    def on_delegation(self, task, result, *, child_session_id="", **kwargs) -> None:
        self.calls.append(("on_delegation", task))

    def on_memory_write(self, action, target, content, metadata=None) -> None:
        self.calls.append(("on_memory_write", action))

    def shutdown(self) -> None:
        self.calls.append(("shutdown", None))

    # -- assertion helpers ---------------------------------------------------
    def write_calls(self) -> List[tuple]:
        return [c for c in self.calls if c[0] in WRITE_HOOKS]

    def read_calls(self) -> List[tuple]:
        return [c for c in self.calls if c[0] == "handle_tool_call"]


def _make_manager() -> tuple:
    provider = _InstrumentedProvider()
    mm = MemoryManager()
    mm.add_provider(provider)
    return mm, provider


# ---------------------------------------------------------------------------
# Flag helper semantics
# ---------------------------------------------------------------------------

class TestMemoryIngestAllowed:
    def test_default_missing_attr_allows(self):
        assert memory_ingest_allowed(types.SimpleNamespace()) is True

    def test_explicit_false_allows(self):
        assert memory_ingest_allowed(
            types.SimpleNamespace(_memory_ingest_disabled=False)
        ) is True

    def test_disabled_blocks(self):
        assert memory_ingest_allowed(
            types.SimpleNamespace(_memory_ingest_disabled=True)
        ) is False


# ---------------------------------------------------------------------------
# Prologue leak sites: on_turn_start + prefetch_all (turn_context.py)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _stub_runtime_main():
    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
        yield


def _build_prologue(agent, **overrides):
    kwargs = dict(
        agent=agent,
        # Upstream skips external-memory prefetch for trivial greetings; use a
        # substantive query so the control test still exercises that seam.
        user_message="Summarize the project decisions we made last week",
        system_message=None,
        conversation_history=None,
        task_id=None,
        stream_callback=None,
        persist_user_message=None,
        restore_or_build_system_prompt=lambda *a, **k: None,
        install_safe_stdio=lambda: None,
        sanitize_surrogates=lambda s: s,
        summarize_user_message_for_log=lambda s: s,
        set_session_context=lambda _sid: None,
        set_current_write_origin=lambda _o: None,
        ra=lambda: types.SimpleNamespace(_set_interrupt=lambda *a, **k: None),
    )
    kwargs.update(overrides)
    return build_turn_context(**kwargs)


def _prologue_agent() -> Any:
    agent = _FakeAgent()
    agent._skip_mcp_refresh = True
    agent._memory_ingest_disabled = False
    return agent


class TestPrologueSites:
    def test_prologue_blocked_when_ingest_disabled(self):
        agent = _prologue_agent()
        agent._memory_manager, provider = _make_manager()
        agent._memory_ingest_disabled = True

        _build_prologue(agent)

        assert provider.write_calls() == [], (
            f"prologue leaked into provider: {provider.write_calls()}"
        )

    def test_prologue_fires_when_flag_off(self):
        """Control: the instrumentation must actually catch the calls."""
        agent = _prologue_agent()
        agent._memory_manager, provider = _make_manager()

        _build_prologue(agent)

        kinds = [c[0] for c in provider.write_calls()]
        assert "on_turn_start" in kinds
        assert "prefetch" in kinds


# ---------------------------------------------------------------------------
# Sync chokepoint + session boundary paths (run_agent.py, bare AIAgent)
# ---------------------------------------------------------------------------

def _bare_agent() -> Any:
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent.session_id = "test-session"
    agent.context_compressor = None
    return agent


class TestSyncChokepoint:
    def test_sync_blocked_when_ingest_disabled(self):
        from run_agent import AIAgent

        agent = _bare_agent()
        agent._memory_manager, provider = _make_manager()
        agent._memory_ingest_disabled = True

        AIAgent._sync_external_memory_for_turn(
            agent,
            original_user_message="user msg",
            final_response="assistant msg",
            interrupted=False,
            messages=[{"role": "user", "content": "user msg"}],
        )
        agent._memory_manager.flush_pending(timeout=5)

        assert provider.write_calls() == []

    def test_sync_fires_when_flag_off(self):
        from run_agent import AIAgent

        agent = _bare_agent()
        agent._memory_manager, provider = _make_manager()
        agent._memory_ingest_disabled = False

        AIAgent._sync_external_memory_for_turn(
            agent,
            original_user_message="user msg",
            final_response="assistant msg",
            interrupted=False,
            messages=[{"role": "user", "content": "user msg"}],
        )
        agent._memory_manager.flush_pending(timeout=5)

        kinds = [c[0] for c in provider.write_calls()]
        assert "sync_turn" in kinds
        assert "queue_prefetch" in kinds

    def test_commit_memory_session_blocked(self):
        from run_agent import AIAgent

        agent = _bare_agent()
        agent._memory_manager, provider = _make_manager()
        agent._memory_ingest_disabled = True

        AIAgent.commit_memory_session(agent, [{"role": "user", "content": "x"}])

        assert provider.write_calls() == []

    def test_shutdown_memory_provider_blocked(self):
        """An ingest-disabled fork never owns its (rebound) manager: neither
        end-of-session extraction nor provider teardown may fire from it."""
        from run_agent import AIAgent

        agent = _bare_agent()
        agent._memory_manager, provider = _make_manager()
        agent._memory_ingest_disabled = True

        AIAgent.shutdown_memory_provider(agent, [{"role": "user", "content": "x"}])

        assert provider.write_calls() == []

    def test_shutdown_memory_provider_fires_when_flag_off(self):
        from run_agent import AIAgent

        agent = _bare_agent()
        agent._memory_manager, provider = _make_manager()
        agent._memory_ingest_disabled = False

        AIAgent.shutdown_memory_provider(agent, [{"role": "user", "content": "x"}])

        kinds = [c[0] for c in provider.write_calls()]
        assert "on_session_end" in kinds

    def test_memory_search_read_stays_allowed(self):
        """Reads are deliberately NOT gated: the whole point of rebinding the
        manager onto the fork is memory_search access."""
        agent = _bare_agent()
        agent._memory_manager, provider = _make_manager()
        agent._memory_ingest_disabled = True

        assert agent._memory_manager.has_tool("memory_search")
        result = agent._memory_manager.handle_tool_call(
            "memory_search", {"query": "anything"}
        )

        assert json.loads(result)["success"] is True
        assert provider.read_calls() == [("handle_tool_call", "memory_search")]
        assert provider.write_calls() == []


# ---------------------------------------------------------------------------
# Delegation observations (tools/delegate_tool.py)
# ---------------------------------------------------------------------------

class TestDelegationSite:
    def test_delegation_gate_checks_the_flag(self):
        """``_finalize_child_results`` records child outcomes as provider
        observations; an ingest-disabled parent must not."""
        import inspect

        from tools.delegate_tool import _finalize_child_results

        src = inspect.getsource(_finalize_child_results)
        assert "memory_ingest_allowed(parent_agent)" in src


# ---------------------------------------------------------------------------
# Fork construction: background review must set the flag
# ---------------------------------------------------------------------------

def test_background_review_fork_sets_ingest_disabled(monkeypatch):
    """The fork construction recipe (ADR-004 §4.1) must set the flag BEFORE
    run_conversation — skip_memory alone is not the systematic guarantee once
    a parent manager is rebound onto the fork.

    Port note: upstream drove this through ``AIAgent._spawn_background_review``
    with ``run_agent.threading.Thread`` monkeypatched to run inline. On our
    base that method hands the work to a process-wide review coordinator, so
    we build the thread target via ``spawn_background_review_thread`` and call
    it directly — same fork-construction code path, no coordinator in the way.
    """
    import datetime as _dt

    import run_agent as run_agent_module
    from agent.background_review import spawn_background_review_thread

    seen = {}

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            self._session_messages = []
            # Default matches AIAgent.__init__ (agent_init.py): ingest allowed.
            self._memory_ingest_disabled = False

        def run_conversation(self, **kwargs):
            seen["ingest_disabled_at_run_time"] = self._memory_ingest_disabled
            return {"final_response": "", "messages": []}

        def shutdown_memory_provider(self, *_a, **_k):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)

    agent = types.SimpleNamespace(
        model="fake-model",
        platform="cli",
        provider="openai",
        base_url="",
        api_key="",
        api_mode="",
        session_id="test-session",
        _parent_session_id="",
        _credential_pool=None,
        _memory_store=object(),
        _memory_enabled=True,
        _user_profile_enabled=False,
        _memory_nudge_interval=0,
        _cached_system_prompt="test-cached-system-prompt",
        session_start=_dt.datetime(2026, 1, 1, 12, 0, 0),
        background_review_callback=None,
        status_callback=None,
        enabled_toolsets=None,
        disabled_toolsets=None,
        request_overrides={},
        reasoning_config=None,
        ephemeral_system_prompt=None,
        prefill_messages=None,
        _safe_print=lambda *_a, **_k: None,
        _current_main_runtime=lambda: {},
    )

    target, _prompt = spawn_background_review_thread(
        agent,
        messages_snapshot=[{"role": "user", "content": "hello"}],
        review_memory=True,
    )
    target()

    assert seen.get("ingest_disabled_at_run_time") is True


# ---------------------------------------------------------------------------
# CI-level guarantee: a full simulated run produces ZERO ingest calls
# ---------------------------------------------------------------------------

class _MockHandler(BaseHTTPRequestHandler):
    response_queue: list = []

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode())
        is_stream = req.get("stream") is True
        if type(self).response_queue:
            resp = type(self).response_queue.pop(0)
        else:
            resp = _text_resp("DONE")
        msg = resp["choices"][0]["message"]
        if is_stream:
            content = msg.get("content") or ""
            tcs = msg.get("tool_calls")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [{"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}]
            if content:
                chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]})
            if tcs:
                for ti, tc in enumerate(tcs):
                    chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"tool_calls": [{
                        "index": ti, "id": tc["id"], "type": "function",
                        "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}]}, "finish_reason": None}]})
            chunks.append({"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tcs else "stop"}]})
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *a, **kw):
        pass


def _tc_resp(name: str, args: str = "{}") -> dict:
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": name, "arguments": args}}]},
            "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    }


def _text_resp(text: str) -> dict:
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    }


@pytest.fixture()
def fork_run_env():
    """Mock provider + isolated HERMES_HOME; yields a factory building the
    ingest-curator fork recipe: skip_memory fork + rebound instrumented
    manager + the flag."""
    _MockHandler.response_queue = []
    srv = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    test_home = tempfile.mkdtemp(prefix="hermes_ingest_disabled_")
    prev_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = os.path.join(test_home, ".hermes")
    os.makedirs(os.environ["HERMES_HOME"])

    from run_agent import AIAgent

    def make_fork(ingest_disabled: bool):
        agent = AIAgent(
            api_key="test-key", base_url=f"http://127.0.0.1:{port}/v1",
            provider="openai-compat", model="test-model",
            max_iterations=6, enabled_toolsets=[],
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            save_trajectories=False, platform="cli",
        )
        # The ADR §4.1 rebinding recipe: reads via the parent's manager.
        mm, provider = _make_manager()
        agent._memory_manager = mm
        agent._memory_ingest_disabled = ingest_disabled
        agent.valid_tool_names = set(agent.valid_tool_names or set())
        agent.valid_tool_names.add("memory_search")
        return agent, mm, provider

    try:
        yield make_fork, _MockHandler
    finally:
        srv.shutdown()
        shutil.rmtree(test_home, ignore_errors=True)
        if prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev_home


class TestForkSimulatedRun:
    def test_zero_ingest_calls_across_full_run_with_reads_allowed(self, fork_run_env):
        """The ADR's CI gate: a full run_conversation on an ingest-disabled
        fork — including a memory_search tool round-trip — must record ZERO
        write/ingest/prefetch provider calls while the read succeeds."""
        make_fork, handler = fork_run_env
        agent, mm, provider = make_fork(ingest_disabled=True)

        handler.response_queue.append(_tc_resp("memory_search", '{"query": "past work"}'))
        handler.response_queue.append(_text_resp("done"))

        result = agent.run_conversation("simulated fork task", conversation_history=[])
        mm.flush_pending(timeout=5)

        assert result["final_response"] == "done"
        # The read went through...
        assert provider.read_calls() == [("handle_tool_call", "memory_search")]
        # ...and nothing else did. Any future hook that re-opens a leak
        # lands here as a named call.
        assert provider.write_calls() == [], (
            f"ingest leak from fork run: {provider.write_calls()}"
        )

    def test_control_run_records_ingest_when_flag_off(self, fork_run_env):
        """Control: the same run with the flag off MUST leak — proving the
        zero-assert above is not vacuous."""
        make_fork, handler = fork_run_env
        agent, mm, provider = make_fork(ingest_disabled=False)

        handler.response_queue.append(_text_resp("done"))

        agent.run_conversation("simulated live turn", conversation_history=[])
        mm.flush_pending(timeout=5)

        kinds = [c[0] for c in provider.write_calls()]
        assert "on_turn_start" in kinds
        assert "prefetch" in kinds
        assert "sync_turn" in kinds


# ---------------------------------------------------------------------------
# CI-level guarantee: a full simulated run produces ZERO ingest calls
# ---------------------------------------------------------------------------

class _MockHandler(BaseHTTPRequestHandler):
    response_queue: list = []

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode())
        is_stream = req.get("stream") is True
        if type(self).response_queue:
            resp = type(self).response_queue.pop(0)
        else:
            resp = _text_resp("DONE")
        msg = resp["choices"][0]["message"]
        if is_stream:
            content = msg.get("content") or ""
            tcs = msg.get("tool_calls")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [{"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}]
            if content:
                chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]})
            if tcs:
                for ti, tc in enumerate(tcs):
                    chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"tool_calls": [{
                        "index": ti, "id": tc["id"], "type": "function",
                        "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}]}, "finish_reason": None}]})
            chunks.append({"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tcs else "stop"}]})
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *a, **kw):
        pass


def _tc_resp(name: str, args: str = "{}") -> dict:
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": name, "arguments": args}}]},
            "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    }


def _text_resp(text: str) -> dict:
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    }


@pytest.fixture()
def fork_run_env():
    """Mock provider + isolated HERMES_HOME; yields a factory building the
    ingest-curator fork recipe: skip_memory fork + rebound instrumented
    manager + the flag."""
    _MockHandler.response_queue = []
    srv = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    test_home = tempfile.mkdtemp(prefix="hermes_ingest_disabled_")
    prev_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = os.path.join(test_home, ".hermes")
    os.makedirs(os.environ["HERMES_HOME"])

    from run_agent import AIAgent

    def make_fork(ingest_disabled: bool):
        agent = AIAgent(
            api_key="test-key", base_url=f"http://127.0.0.1:{port}/v1",
            provider="openai-compat", model="test-model",
            max_iterations=6, enabled_toolsets=[],
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            save_trajectories=False, platform="cli",
        )
        # The ADR §4.1 rebinding recipe: reads via the parent's manager.
        mm, provider = _make_manager()
        agent._memory_manager = mm
        agent._memory_ingest_disabled = ingest_disabled
        agent.valid_tool_names = set(agent.valid_tool_names or set())
        agent.valid_tool_names.add("memory_search")
        return agent, mm, provider

    try:
        yield make_fork, _MockHandler
    finally:
        srv.shutdown()
        shutil.rmtree(test_home, ignore_errors=True)
        if prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev_home


class TestForkSimulatedRun:
    def test_zero_ingest_calls_across_full_run_with_reads_allowed(self, fork_run_env):
        """The ADR's CI gate: a full run_conversation on an ingest-disabled
        fork — including a memory_search tool round-trip — must record ZERO
        write/ingest/prefetch provider calls while the read succeeds."""
        make_fork, handler = fork_run_env
        agent, mm, provider = make_fork(ingest_disabled=True)

        handler.response_queue.append(_tc_resp("memory_search", '{"query": "past work"}'))
        handler.response_queue.append(_text_resp("done"))

        result = agent.run_conversation("simulated fork task", conversation_history=[])
        mm.flush_pending(timeout=5)

        assert result["final_response"] == "done"
        # The read went through...
        assert provider.read_calls() == [("handle_tool_call", "memory_search")]
        # ...and nothing else did. Any future hook that re-opens a leak
        # lands here as a named call.
        assert provider.write_calls() == [], (
            f"ingest leak from fork run: {provider.write_calls()}"
        )

    def test_control_run_records_ingest_when_flag_off(self, fork_run_env):
        """Control: the same run with the flag off MUST leak — proving the
        zero-assert above is not vacuous."""
        make_fork, handler = fork_run_env
        agent, mm, provider = make_fork(ingest_disabled=False)

        handler.response_queue.append(_text_resp("done"))

        agent.run_conversation("Summarize the project decisions we made last week", conversation_history=[])
        mm.flush_pending(timeout=5)

        kinds = [c[0] for c in provider.write_calls()]
        assert "on_turn_start" in kinds
        assert "prefetch" in kinds
        assert "sync_turn" in kinds

    def test_fork_memory_search_does_not_touch_taint_registry(
        self, fork_run_env, tmp_path
    ):
        """ADR-004 §① origin-taint: an ingest-disabled fork shares the
        parent's LIVE session_id, so its whitelisted memory_search reads must
        not register injected spans in the shared taint registry — the
        shadow observer's reads would otherwise mutate live confirm-time
        admission outcomes (registry side-channel)."""
        import agent.memory_taint as mt
        from agent.memory_taint import TaintRegistry

        make_fork, handler = fork_run_env
        reg = TaintRegistry(base_dir=tmp_path / "taint")
        mt.set_registry(reg)
        try:
            agent, mm, provider = make_fork(ingest_disabled=True)
            handler.response_queue.append(
                _tc_resp("memory_search", '{"query": "past work"}')
            )
            handler.response_queue.append(_text_resp("done"))

            agent.run_conversation("simulated fork task", conversation_history=[])
            mm.flush_pending(timeout=5)

            # The read itself went through...
            assert provider.read_calls() == [("handle_tool_call", "memory_search")]
            # ...but nothing was registered for the (shared) session.
            sid = getattr(agent, "session_id", "") or ""
            assert reg.session_injected_digest(sid)["count"] == 0
        finally:
            mt.set_registry(None)

    def test_live_agent_memory_search_registers_taint(
        self, fork_run_env, tmp_path
    ):
        """Control (and dispatch-coverage regression): a live agent's
        memory_search result MUST land in the taint registry regardless of
        which dispatch path executed the tool — if memory tools are ever
        added to the parallel-safe allowlist, this end-to-end assert catches
        an unregistered path."""
        import agent.memory_taint as mt
        from agent.memory_taint import TaintRegistry

        make_fork, handler = fork_run_env
        reg = TaintRegistry(base_dir=tmp_path / "taint")
        mt.set_registry(reg)
        try:
            agent, mm, provider = make_fork(ingest_disabled=False)
            handler.response_queue.append(
                _tc_resp("memory_search", '{"query": "past work"}')
            )
            handler.response_queue.append(_text_resp("done"))

            agent.run_conversation("simulated live turn", conversation_history=[])
            mm.flush_pending(timeout=5)

            sid = getattr(agent, "session_id", "") or ""
            digest = reg.session_injected_digest(sid)
            assert digest["count"] >= 1, digest
            assert "memory_search" in digest["sources"], digest
        finally:
            mt.set_registry(None)
