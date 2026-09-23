"""The manager's ordered writer owns durable memory evidence, not the provider."""

import json

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def _records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class RecordingProvider(MemoryProvider):
    def __init__(self, name="external", *, fail=False, before_sync=None):
        self._name = name
        self.fail = fail
        self.before_sync = before_sync
        self.calls = []

    @property
    def name(self):
        return self._name

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        pass

    def get_tool_schemas(self):
        return []

    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None, turn_author=None):
        if self.before_sync:
            self.before_sync()
        self.calls.append((user_content, assistant_content, session_id, messages, turn_author))
        if self.fail:
            raise RuntimeError("ingest failed")


def test_completed_turn_is_journaled_before_dispatch_and_acked_after_success(tmp_path):
    token = set_hermes_home_override(tmp_path)
    try:
        wal_path = tmp_path / "state" / "memory-pending" / "turn-1.jsonl"
        mirror_dir = tmp_path / "memory" / "l0-mirror"

        def before_sync():
            assert [record["type"] for record in _records(wal_path)] == ["turn"]
            assert [record["kind"] for path in mirror_dir.glob("*.jsonl")
                    for record in _records(path)] == ["sync_turn"]

        manager = MemoryManager()
        provider = RecordingProvider(before_sync=before_sync)
        manager.add_provider(provider)
        messages = [{"role": "user", "content": "request"}]
        author = {"id": "member-1", "name": "Member", "is_bot": False}
        manager.sync_all("request", "answer", session_id="turn-1", messages=messages, turn_author=author)
        assert manager.flush_pending(timeout=5)

        wal = _records(wal_path)
        mirror = [record for path in mirror_dir.glob("*.jsonl") for record in _records(path)]
        assert [record["type"] for record in wal] == ["turn", "ack"]
        assert wal[0]["id"] == wal[1]["id"] == mirror[0]["wal_entry_id"]
        assert mirror[0]["meta"]["providers"] == ["external"]
        assert provider.calls == [("request", "answer", "turn-1", messages, author)]
        manager.shutdown_all()
    finally:
        reset_hermes_home_override(token)


def test_failed_provider_leaves_wal_pending_but_does_not_block_the_next_provider(tmp_path):
    token = set_hermes_home_override(tmp_path)
    try:
        manager = MemoryManager()
        failed = RecordingProvider("builtin", fail=True)
        succeeded = RecordingProvider("external")
        manager.add_provider(failed)
        manager.add_provider(succeeded)
        manager.sync_all("request", "answer", session_id="turn-2")
        assert manager.flush_pending(timeout=5)

        wal = _records(tmp_path / "state" / "memory-pending" / "turn-2.jsonl")
        assert [record["type"] for record in wal] == ["turn"]
        assert len(succeeded.calls) == 1
        manager.shutdown_all()
    finally:
        reset_hermes_home_override(token)


def test_journals_remain_in_their_constructing_profile_across_a_b_a(tmp_path):
    homes = [tmp_path / "a", tmp_path / "b"]
    managers = []
    try:
        for home in homes:
            token = set_hermes_home_override(home)
            try:
                manager = MemoryManager()
                manager.add_provider(RecordingProvider())
                managers.append(manager)
            finally:
                reset_hermes_home_override(token)

        for home, manager, session in ((homes[0], managers[0], "a-1"),
                                       (homes[1], managers[1], "b-1"),
                                       (homes[0], managers[0], "a-2")):
            token = set_hermes_home_override(home)
            try:
                manager.sync_all(session, "answer", session_id=session)
                assert manager.flush_pending(timeout=5)
            finally:
                reset_hermes_home_override(token)

        assert {p.stem for p in (homes[0] / "state" / "memory-pending").glob("*.jsonl")} == {"a-1", "a-2"}
        assert {p.stem for p in (homes[1] / "state" / "memory-pending").glob("*.jsonl")} == {"b-1"}
        for home in homes:
            mirror = [record for path in (home / "memory" / "l0-mirror").glob("*.jsonl")
                      for record in _records(path)]
            assert len(mirror) == (2 if home == homes[0] else 1)
    finally:
        for manager in managers:
            manager.shutdown_all()


def test_session_and_compression_boundaries_are_content_free_markers(tmp_path):
    token = set_hermes_home_override(tmp_path)
    try:
        manager = MemoryManager()
        manager.add_provider(RecordingProvider())
        messages = [{"role": "user", "content": "private boundary content"}]
        manager.on_pre_compress(messages)
        manager.on_session_end(messages)
        assert manager.flush_pending(timeout=5)

        mirror = [record for path in (tmp_path / "memory" / "l0-mirror").glob("*.jsonl")
                  for record in _records(path)]
        assert [record["kind"] for record in mirror] == ["pre_compress", "session_end"]
        assert all("private boundary content" not in json.dumps(record) for record in mirror)
        assert all(record["skeleton"][0]["role"] == "user" for record in mirror)
        manager.shutdown_all()
    finally:
        reset_hermes_home_override(token)
