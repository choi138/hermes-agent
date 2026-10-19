"""A human message queued behind background-process wakes runs next, and wakes batch into one turn.

Field failure (Discord thread, 2026-10-19): a long task launched several background processes
with completion + watch notifications. Each wake took its own turn, strictly FIFO, so replies to
the human ("approved", "did any lane go past round 2?") came 3-47 minutes late, and in between
the agent kept saying it was still waiting for the approval it had already been given.
"""

from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.pending_priority import absorb_process_notifications, discard_human_keep_wakes
from gateway.platforms.base import BasePlatformAdapter, pop_pending_message_event
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from gateway.turn_context import TurnContext


class Adapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, text, **kwargs):
        pass

    async def get_chat_info(self, chat_id):
        return {}


SOURCE = SessionSource(platform=Platform.DISCORD, chat_id="thread-1", user_id="human", chat_type="thread")
KEY = build_session_key(SOURCE)
SECURITY_KEYS = GatewayRunner._SECURITY_METADATA_KEYS


def _human(text):
    return MessageEvent(text=text, source=SOURCE, message_id=f"m-{text}")


def _wake(text, **metadata):
    return MessageEvent(text=text, source=SOURCE, internal=True,
                        metadata={"notification_origin": "process_registry_synthetic", **metadata})


def _runner():
    runner = object.__new__(GatewayRunner)
    runner._draining = False
    return runner


def _adapter():
    return Adapter(PlatformConfig(enabled=True), Platform.DISCORD)


async def _drain_all(runner, adapter):
    texts = []
    result = {"final_response": "done"}
    for _ in range(40):
        event, text = await runner._run_agent_drain_pending(result, adapter, SOURCE, KEY)
        if not event and not text:
            return texts
        texts.append(text)
    raise AssertionError("queue never drained")


@pytest.mark.asyncio
async def test_human_queued_behind_wakes_runs_before_them_and_humans_keep_order():
    runner, adapter = _runner(), _adapter()
    for event in (_wake("wake-1"), _wake("wake-2"), _human("approved"),
                  _wake("wake-3"), _human("did any lane pass round 2?")):
        runner._enqueue_fifo(KEY, event, adapter)

    texts = await _drain_all(runner, adapter)

    assert texts[:2] == ["approved", "did any lane pass round 2?"]
    assert all("wake" in text for text in texts[2:])
    assert sorted(" ".join(texts[2:]).split()) == ["wake-1", "wake-2", "wake-3"]
    assert runner._queue_depth(KEY, adapter=adapter) == 0


def test_pop_prefers_human_in_followup_chain_but_keeps_recovery_ticket_first():
    pending = {}
    ticket, wake, human = _wake("ticket"), _wake("wake"), _human("hi")
    ticket._hermes_turn_resume = {"turn_id": "t-1"}
    ticket._gateway_pending_followups = [wake, human]
    pending[KEY] = ticket

    assert pop_pending_message_event(pending, KEY) is ticket
    assert pop_pending_message_event(pending, KEY) is human
    assert pop_pending_message_event(pending, KEY) is wake
    assert pop_pending_message_event(pending, KEY) is None
    assert not any(hasattr(event, "_gateway_pending_followups") for event in (ticket, wake, human))


@pytest.mark.asyncio
async def test_wake_burst_cannot_fill_the_queue_a_human_needs():
    runner, adapter = _runner(), _adapter()
    runner._delivery_adapter_for = lambda source: adapter
    wakes = [_wake(f"wake-{i}") for i in range(runner._BUSY_QUEUE_MAX_PENDING)]
    for wake in wakes:
        runner._queue_or_replace_pending_event(KEY, wake)
    internal_cap = runner._BUSY_QUEUE_MAX_PENDING - runner._BUSY_QUEUE_HUMAN_RESERVE

    assert sum(wake._gateway_accepted for wake in wakes) == internal_cap
    # Refused wakes stay unaccepted, so their producer keeps the claim and retries.
    assert not any(wake._gateway_accepted for wake in wakes[internal_cap:])

    humans = [_human(f"human-{i}") for i in range(runner._BUSY_QUEUE_HUMAN_RESERVE)]
    for human in humans:
        runner._queue_or_replace_pending_event(KEY, human)
    assert all(human._gateway_accepted for human in humans)
    event, _ = await runner._run_agent_drain_pending({"final_response": "done"}, adapter, SOURCE, KEY)
    assert event is humans[0]


@pytest.mark.asyncio
async def test_queued_plain_notifications_batch_into_one_turn():
    runner, adapter = _runner(), _adapter()
    runner._enqueue_fifo(KEY, _human("busy turn"), adapter)
    pop_pending_message_event(adapter._pending_messages, KEY)
    for i in range(3):
        runner._enqueue_fifo(KEY, _wake(f"watch-{i}"), adapter)

    texts = await _drain_all(runner, adapter)

    assert texts == ["watch-0\n\nwatch-1\n\nwatch-2"]


def test_ledger_bound_wakes_each_keep_their_own_turn():
    for marker in ("process_followup", "process_continuation"):
        pending = {}
        head = _wake("ledger head", **{marker: {"execution_id": "p1", "token": "a"}})
        plain = _wake("watch")
        pending[KEY] = plain
        assert absorb_process_notifications(pending, KEY, head, SECURITY_KEYS) == 0

        ledger_tail = _wake("ledger tail", **{marker: {"execution_id": "p2", "token": "b"}})
        pending[KEY] = ledger_tail
        assert absorb_process_notifications(pending, KEY, _wake("watch-head"), SECURITY_KEYS) == 0
        assert pop_pending_message_event(pending, KEY) is ledger_tail


def test_stop_drops_one_human_follow_up_and_keeps_every_wake():
    pending, overflow = {}, []
    wake_a, human, wake_b, wake_c = _wake("a"), _human("stale"), _wake("b"), _wake("c")
    pending[KEY] = wake_a
    wake_a._gateway_pending_followups = [human, wake_b]
    overflow.append(wake_c)

    assert discard_human_keep_wakes(pending, KEY, overflow) is human
    assert [pop_pending_message_event(pending, KEY) for _ in range(2)] == [wake_a, wake_b]
    assert overflow == [wake_c]

    pending, overflow = {KEY: _human("only")}, [_human("later"), wake_c]
    discard_human_keep_wakes(pending, KEY, overflow)
    assert pending[KEY] is wake_c and [event.text for event in overflow] == ["later"]


def test_wakes_with_different_security_context_or_delegation_claims_stay_apart():
    pending = {}
    head = _wake("watch")
    foreign = _wake("plugin", hermes_plugin_id="other")
    delegation = _wake("delegation", gateway_completion_ids=["d1"])
    pending[KEY] = foreign
    foreign._gateway_pending_followups = [delegation]

    assert absorb_process_notifications(pending, KEY, head, SECURITY_KEYS) == 0
    assert head.text == "watch"


@pytest.mark.asyncio
async def test_depth_cap_requeue_keeps_the_queued_successor():
    runner, adapter = _runner(), _adapter()
    runner._delivery_adapter_for = lambda source: adapter
    deferred, successor = _human("first"), _human("second")
    adapter._pending_messages[KEY] = successor
    ctx = TurnContext(source=SOURCE, context_prompt="", channel_prompt=None, session_key=KEY,
                      session_id="s-1", run_generation=1, history=[])
    ctx._interrupt_depth = runner._MAX_INTERRUPT_DEPTH
    runner._run_agent_deliver_first_response = AsyncMock()

    await runner._run_agent_queued_followup(ctx, adapter, "first", deferred,
                                            {"final_response": "ok"}, {"final_response": "ok"}, None)

    assert pop_pending_message_event(adapter._pending_messages, KEY) is deferred
    assert pop_pending_message_event(adapter._pending_messages, KEY) is successor
