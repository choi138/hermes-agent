from __future__ import annotations

import asyncio

from gateway.config import Platform
from gateway.session import SessionContext, SessionSource
from gateway.session_context import (
    clear_session_vars,
    get_trusted_gateway_source,
    reset_session_vars,
    set_session_vars,
)


def test_trusted_source_never_falls_back_to_process_environment(monkeypatch):
    reset_session_vars()
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_PROFILE", "default")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "forged-user")

    assert get_trusted_gateway_source() is None


def test_regular_session_binding_is_not_implicitly_trusted():
    tokens = set_session_vars(
        platform="discord",
        profile="default",
        chat_id="thread-1",
        user_id="user-1",
        message_id="message-1",
    )
    try:
        assert get_trusted_gateway_source() is None
    finally:
        clear_session_vars(tokens)


def test_gateway_binding_carries_authoritative_identity_and_threadsafe_wake():
    from gateway.run import GatewayRunner

    async def scenario():
        runner = object.__new__(GatewayRunner)
        runner.adapters = {}
        runner._active_profile_name = lambda: "default"
        runner._gateway_loop = asyncio.get_running_loop()
        runner._kanban_dispatch_wake_event = asyncio.Event()
        source = SessionSource(
            platform=Platform.DISCORD,
            chat_id="thread-1",
            chat_type="thread",
            user_id="user-1",
            thread_id="thread-1",
            scope_id="guild-1",
            parent_chat_id="channel-1",
            message_id="message-1",
            role_authorized=True,
        )
        context = SessionContext(
            source=source,
            connected_platforms=[Platform.DISCORD],
            home_channels={},
            session_key="discord:thread-1",
            session_id="session-1",
        )

        tokens = runner._set_session_env(context)
        try:
            trusted = get_trusted_gateway_source()
            assert trusted is not None
            assert trusted.platform == "discord"
            assert trusted.profile == "default"
            assert trusted.chat_id == "thread-1"
            assert trusted.thread_id == "thread-1"
            assert trusted.scope_id == "guild-1"
            assert trusted.parent_chat_id == "channel-1"
            assert trusted.user_id == "user-1"
            assert trusted.session_key == "discord:thread-1"
            assert trusted.session_id == "session-1"
            assert trusted.message_id == "message-1"
            assert trusted.role_authorized is True
            assert trusted.dispatch_wake is not None

            trusted.dispatch_wake()
            await asyncio.sleep(0)
            assert runner._kanban_dispatch_wake_event.is_set()
        finally:
            runner._clear_session_env(tokens)

        assert get_trusted_gateway_source() is None

    asyncio.run(scenario())
