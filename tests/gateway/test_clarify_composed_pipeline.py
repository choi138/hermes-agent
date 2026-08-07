"""Composed regression coverage for clarify replies crossing the adapter and runner.

The narrow tests in this area cover the clarify state machine, the adapter's
active-session bypass, and the runner's prose rejection independently.  This
module keeps those seams connected: a native choice prompt is pending while
both the adapter session guard and the runner's active-agent slot are live.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource, build_session_key


class _CaptureAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.SLACK)
        self.sent: list[str] = []

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return SendResult(success=True, message_id=f"m{len(self.sent)}")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "im"}


class _RunningAgent:
    def __init__(self):
        self.interrupts: list[str] = []
        self._active_children: list[object] = []

    def interrupt(self, text):
        self.interrupts.append(text)

    def get_activity_summary(self):
        return {
            "seconds_since_activity": 0,
            "last_activity_desc": "waiting for clarify",
            "api_call_count": 1,
            "max_iterations": 10,
        }


def _event(text: str, message_id: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.SLACK,
            chat_id="D123",
            chat_type="dm",
            user_id="U1",
            thread_id="1111.2222",
        ),
        message_id=message_id,
    )


def _clear_clarify_state():
    from tools import clarify_gateway as cm

    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["2", "Beta"])
async def test_native_choice_preserves_prose_then_drains_it_once_after_selection(selection):
    """Rejected prose is acknowledged and saved without stealing the clarify turn."""
    _clear_clarify_state()
    from gateway.run import GatewayRunner
    from tools import clarify_gateway as cm

    adapter = _CaptureAdapter()
    initial = _event("start", "initial")
    session_key = build_session_key(
        initial.source,
        group_sessions_per_user=adapter.config.extra.get("group_sessions_per_user", True),
        thread_sessions_per_user=adapter.config.extra.get("thread_sessions_per_user", False),
    )

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._startup_restore_in_progress = False
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._is_user_authorized = lambda source: True
    runner._session_key_for_source = lambda source: session_key
    runner._adapter_for_source = lambda source: adapter
    runner._update_prompt_pending = {}
    runner._draining = False
    # The clarify reply must not inherit the operator's normal interrupt policy.
    runner._busy_input_mode = "interrupt"
    runner._busy_text_mode = "interrupt"

    running_agent = _RunningAgent()
    prompt_ready = asyncio.Event()
    followup_drained = asyncio.Event()
    handled_followups: list[str] = []

    async def composed_handler(event: MessageEvent):
        if event.message_id == "initial":
            state = runner._session_state(session_key)
            state.turn.agent = running_agent
            state.turn.started_ts = time.time()
            cm.register(
                "clarify-composed",
                session_key,
                "Choose a release channel",
                ["Alpha", "Beta"],
            )
            prompt_ready.set()
            answer = await asyncio.to_thread(
                cm.wait_for_response,
                "clarify-composed",
                5.0,
            )
            runner._release_running_agent_state(session_key)
            return f"selected:{answer}"

        if runner._is_session_running(session_key):
            return await runner._handle_message(event)

        handled_followups.append(event.text)
        followup_drained.set()
        return f"handled:{event.text}"

    adapter._message_handler = composed_handler

    try:
        await adapter.handle_message(initial)
        await asyncio.wait_for(prompt_ready.wait(), timeout=5)

        prose = "Please also include the migration notes"
        await adapter.handle_message(_event(prose, "prose"))
        await adapter.handle_message(_event(selection, "selection"))

        await asyncio.wait_for(followup_drained.wait(), timeout=5)
        expected_followup_reply = f"handled:{prose}"

        async def _wait_for_followup_delivery():
            while expected_followup_reply not in adapter.sent:
                await asyncio.sleep(0)

        await asyncio.wait_for(_wait_for_followup_delivery(), timeout=5)

        guidance = [message for message in adapter.sent if "saved" in message.lower()]
        assert len(guidance) == 1
        assert "1. Alpha" in guidance[0]
        assert "2. Beta" in guidance[0]
        assert "Other" in guidance[0]
        assert handled_followups == [prose]
        assert running_agent.interrupts == []
        assert adapter._pending_messages == {}
        assert adapter.sent.count("selected:Beta") == 1
        assert adapter.sent.count(f"handled:{prose}") == 1
        assert cm.get_pending_for_session(session_key, include_choice_prompts=True) is None
    finally:
        cm.clear_session(session_key)
        await adapter.cancel_background_tasks()
        _clear_clarify_state()
