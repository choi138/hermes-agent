"""Tests for the lightweight Slack/Discord channel session-continuity hint.

Salvaged from PR #36220 (metamon-p), ported onto the current SessionStore.

Covers:
- SessionStore records the previous session_id on auto-reset (and only then).
- prev_session_id survives a to_dict() → from_dict() roundtrip (gateway restart).
- build_channel_continuity_note() emits a hint only for Slack/Discord sessions
  that were auto-reset with real prior activity, and stays silent otherwise.
"""

from datetime import datetime, timedelta

import pytest

from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.session import (
    SessionEntry,
    SessionSource,
    SessionStore,
    build_channel_continuity_note,
)


@pytest.fixture()
def _isolated_db(tmp_path, monkeypatch):
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _make_store(tmp_path, policy=None):
    config = GatewayConfig()
    if policy:
        config.default_reset_policy = policy
    return SessionStore(sessions_dir=tmp_path / "sessions", config=config)


def _slack_source(thread_id=None):
    return SessionSource(
        platform=Platform.SLACK,
        chat_id="C123",
        chat_type="thread" if thread_id else "channel",
        user_id="U1",
        thread_id=thread_id,
    )


class _DialogueDB:
    def __init__(self, messages=None, error=None):
        self.messages = list(messages or [])
        self.error = error
        self.calls = []

    def get_recent_dialogue_messages(self, session_id, limit):
        self.calls.append((session_id, limit))
        if self.error is not None:
            raise self.error
        return self.messages


class _StoreWithDB:
    def __init__(self, db):
        self._db = db


# ---------------------------------------------------------------------------
# SessionStore records prev_session_id on auto-reset
# ---------------------------------------------------------------------------

class TestPrevSessionIdCapture:
    def test_prev_session_id_set_on_auto_reset(self, _isolated_db, tmp_path):
        store = _make_store(tmp_path, SessionResetPolicy(mode="idle", idle_minutes=1))
        source = _slack_source(thread_id="T9")

        entry1 = store.get_or_create_session(source)
        assert entry1.prev_session_id is None  # fresh session, nothing replaced

        entry1.last_prompt_tokens = 4000  # had real conversation
        entry1.updated_at = datetime.now() - timedelta(minutes=5)
        store._save()

        entry2 = store.get_or_create_session(source)
        assert entry2.was_auto_reset is True
        assert entry2.reset_had_activity is True
        assert entry2.prev_session_id == entry1.session_id


# ---------------------------------------------------------------------------
# build_channel_continuity_note
# ---------------------------------------------------------------------------

def _reset_entry(platform, prev="20260101_000000_abc", had_activity=True):
    return SessionEntry(
        session_key="k",
        session_id="20260101_010000_def",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=platform,
        was_auto_reset=True,
        auto_reset_reason="daily",
        reset_had_activity=had_activity,
        prev_session_id=prev,
    )


class TestBuildChannelContinuityNote:
    def test_slack_channel_emits_hint(self):
        entry = _reset_entry(Platform.SLACK)
        note = build_channel_continuity_note(entry, _slack_source())
        assert note is not None
        assert "session_search" in note
        assert entry.prev_session_id in note
        assert "channel" in note


    def test_no_activity_returns_none(self):
        entry = _reset_entry(Platform.SLACK, had_activity=False)
        assert build_channel_continuity_note(entry, _slack_source()) is None

    def test_success_appends_bounded_last_exchange_digest(
        self, _isolated_db, tmp_path
    ):
        triggering_prefix = (
            "[Triggering message id: `1542732296973647893` — use as `message_id` "
            "for reply/react/pin via the discord tools.]"
        )
        messages = [
            {"role": "user", "content": "old user exchange"},
            {"role": "assistant", "content": "old assistant exchange"},
            {
                "role": "user",
                "content": f"{triggering_prefix}\n\n" + "u" * 205 + "\ncontinued",
            },
            {"role": "assistant", "content": "a" * 305 + "\ncontinued"},
            {"role": "user", "content": "second user\nmessage"},
            {"role": "assistant", "content": "second assistant\nmessage"},
            {"role": "user", "content": "third user message"},
            {"role": "assistant", "content": "third assistant message"},
        ]
        entry = _reset_entry(Platform.SLACK)
        store = _make_store(tmp_path)
        try:
            db = store._db
            assert db is not None
            db.create_session(entry.prev_session_id, "slack")
            for message in messages:
                db.append_message(
                    entry.prev_session_id,
                    message["role"],
                    message["content"],
                )
            note = build_channel_continuity_note(
                entry,
                _slack_source(),
                session_store=store,
            )
        finally:
            store.close_all_db_handles()

        assert note is not None
        assert "Last exchanges before reset:" in note
        assert "old user exchange" not in note
        assert triggering_prefix not in note
        assert "USER: second user message" in note
        assert "ASSISTANT: second assistant message" in note
        digest_lines = note.splitlines()[2:-1]
        assert len(digest_lines) == 6
        first_user = digest_lines[0].removeprefix("USER: ")
        first_assistant = digest_lines[1].removeprefix("ASSISTANT: ")
        assert len(first_user) == 200 and first_user.endswith("…")
        assert len(first_assistant) == 300 and first_assistant.endswith("…")

    def test_message_load_failure_returns_pointer_only_note(self):
        entry = _reset_entry(Platform.SLACK)
        pointer_only = build_channel_continuity_note(entry, _slack_source())
        db = _DialogueDB(error=RuntimeError("state db unavailable"))

        note = build_channel_continuity_note(
            entry,
            _slack_source(),
            session_store=_StoreWithDB(db),
        )

        assert note == pointer_only
        assert "Last exchanges before reset:" not in note

    def test_platform_mismatch_returns_none_without_loading(self):
        entry = _reset_entry(Platform.TELEGRAM)
        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="123",
            chat_type="group",
            user_id="U1",
        )
        db = _DialogueDB(error=AssertionError("must not load"))

        assert (
            build_channel_continuity_note(
                entry,
                source,
                session_store=_StoreWithDB(db),
            )
            is None
        )
        assert db.calls == []
