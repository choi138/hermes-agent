"""Gateway bridge for approved mention-inbox execution receipts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.run import (
    _compose_mention_inbox_execution_callbacks,
    _constrain_mention_inbox_toolsets,
    _install_mention_inbox_pretool_guard,
    _mention_inbox_execution_id,
    _mention_inbox_session_source,
)
from gateway.config import Platform
from gateway.session import SessionSource, build_session_key


def _event(*, internal: bool, execution_id: str = "wx_" + "a" * 24):
    return SimpleNamespace(
        internal=internal,
        metadata={
            "mention_inbox_execution": {
                "execution_id": execution_id,
                "proposal_hash": "b" * 64,
                "mode": "direct",
            }
        },
    )


def test_execution_identity_requires_internal_code_owned_event() -> None:
    expected = "wx_" + "a" * 24
    assert _mention_inbox_execution_id(_event(internal=True)) == expected
    assert _mention_inbox_execution_id(_event(internal=False)) is None
    assert (
        _mention_inbox_execution_id(_event(internal=True, execution_id="wx_not-valid"))
        is None
    )
    assert (
        _mention_inbox_execution_id(SimpleNamespace(internal=True, metadata={})) is None
    )


def test_approved_execution_uses_an_isolated_session_source() -> None:
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="1531851208858275860",
        chat_type="group",
        thread_id="1531936968420884481",
        user_id="999",
    )

    isolated = _mention_inbox_session_source(_event(internal=True), source)

    assert isolated is not source
    assert source.thread_id == "1531936968420884481"
    assert isolated.chat_id == source.chat_id
    assert isolated.thread_id == ("1531936968420884481:approved:wx_" + "a" * 24)
    assert build_session_key(isolated) != build_session_key(source)
    assert _mention_inbox_session_source(_event(internal=False), source) is source


def test_execution_callbacks_preserve_voice_and_forward_sanitized_receipts() -> None:
    calls = []

    class Observer:
        def tool_started(self, execution_id, tool_name):
            calls.append(("observer-start", execution_id, tool_name))

        def tool_completed(self, execution_id, tool_name, result):
            calls.append(("observer-complete", execution_id, tool_name, result))

    def voice(call_id, tool_name, args):
        calls.append(("voice", call_id, tool_name, args))

    execution_id = "wx_" + "c" * 24
    start, complete = _compose_mention_inbox_execution_callbacks(
        execution_id=execution_id,
        observer=Observer(),
        voice_callback=voice,
    )
    result = {"exit_code": 0, "output": "opaque"}

    start("call-1", "terminal", {"command": "pytest"})
    complete("call-1", "terminal", {"command": "pytest"}, result)

    assert calls == [
        ("voice", "call-1", "terminal", {"command": "pytest"}),
        ("observer-complete", execution_id, "terminal", result),
    ]


def test_approved_toolsets_remove_every_unapproved_capability() -> None:
    assert _constrain_mention_inbox_toolsets(
        configured=("file", "terminal", "web", "discord_admin", "code_execution"),
        disabled=(),
        approved=("file", "terminal"),
    ) == ["file", "terminal"]


def test_missing_required_approved_toolset_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="approved execution toolsets unavailable"):
        _constrain_mention_inbox_toolsets(
            configured=("file", "terminal"),
            disabled=(),
            approved=("kanban_submit",),
        )


def test_pretool_guard_commits_receipt_after_existing_guardrail_allows() -> None:
    class Decision:
        allows_execution = True

    class Guardrails:
        def before_call(self, tool_name, args):
            return Decision()

    class Observer:
        def __init__(self):
            self.calls = []

        def tool_started(self, execution_id, tool_name):
            self.calls.append((execution_id, tool_name))

    observer = Observer()
    agent = SimpleNamespace(_tool_guardrails=Guardrails())
    execution_id = "wx_" + "d" * 24

    _install_mention_inbox_pretool_guard(agent, execution_id, observer)
    decision = agent._tool_guardrails.before_call("terminal", {"command": "pytest"})

    assert decision.allows_execution is True
    assert observer.calls == [(execution_id, "terminal")]


def test_pretool_guard_blocks_when_receipt_commit_fails() -> None:
    class Decision:
        allows_execution = True

    class Guardrails:
        def before_call(self, tool_name, args):
            return Decision()

    class Observer:
        def tool_started(self, execution_id, tool_name):
            raise RuntimeError("db unavailable")

    agent = SimpleNamespace(_tool_guardrails=Guardrails())
    _install_mention_inbox_pretool_guard(agent, "wx_" + "e" * 24, Observer())

    decision = agent._tool_guardrails.before_call("terminal", {})

    assert decision.allows_execution is False
    assert decision.action == "block"
    assert "receipt" in decision.message
