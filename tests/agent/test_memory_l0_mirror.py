"""Tests for the L0-mirror — the local evidence journal (ADR-004 §② L0-mirror).

Every payload that leaves for external memory (per-turn sync, end-of-session
extraction input, pre-compression extraction input) is appended to a monthly
JSONL under ``memory/l0-mirror/`` just before ingest submit, so a total graph
loss can be rebuilt from local disk. Scrubbed, fail-open, zero LLM calls.

Covers: the record shape for both payload kinds, monthly file naming, the
scrub, the fail-open contract, and the MemoryManager stub-ingest integration
(mirror written before provider dispatch; broken mirror never blocks sync).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from agent.memory_journal import L0Mirror
from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider


def _month_file(base: Path) -> Path:
    return base / (time.strftime("%Y-%m", time.localtime()) + ".jsonl")


def _records(path: Path) -> List[Dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.fixture()
def mirror(tmp_path) -> L0Mirror:
    return L0Mirror(base_dir=tmp_path / "l0-mirror")


class TestAppendTurn:
    def test_turn_record_shape(self, mirror, tmp_path):
        mirror.append_turn(
            "sess-1", "user asks", "assistant answers",
            provider_names=["hermes-graphiti"], wal_entry_id="abc123",
        )

        recs = _records(_month_file(tmp_path / "l0-mirror"))
        assert len(recs) == 1
        rec = recs[0]
        assert rec["kind"] == "sync_turn"
        assert rec["session_id"] == "sess-1"
        assert rec["wal_entry_id"] == "abc123"
        assert rec["body"] == {"user": "user asks", "assistant": "assistant answers"}
        assert rec["meta"]["providers"] == ["hermes-graphiti"]
        assert isinstance(rec["ts"], float)

    def test_monthly_file_naming(self, mirror, tmp_path):
        mirror.append_turn("s", "u", "a")
        files = [p.name for p in (tmp_path / "l0-mirror").iterdir()]
        assert files == [time.strftime("%Y-%m") + ".jsonl"]

    def test_secrets_are_scrubbed(self, mirror, tmp_path):
        secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        mirror.append_turn("s", f"token {secret}", "ok")
        raw = _month_file(tmp_path / "l0-mirror").read_text(encoding="utf-8")
        assert secret not in raw


class TestAppendMessages:
    def test_session_end_payload_with_tool_calls(self, mirror, tmp_path):
        messages = [
            {"role": "user", "content": "diagnose the failure"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "terminal", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "content": "exit 0"},
            {"role": "assistant", "content": [{"type": "text", "text": "fixed it"}]},
        ]

        mirror.append_messages(
            "session_end", messages, session_id="sess-9",
            provider_names=["hermes-graphiti"],
        )

        rec = _records(_month_file(tmp_path / "l0-mirror"))[0]
        assert rec["kind"] == "session_end"
        assert rec["session_id"] == "sess-9"
        assert rec["meta"]["message_count"] == 4
        roles = [m["role"] for m in rec["body"]]
        assert roles == ["user", "assistant", "tool", "assistant"]
        # Tool-call names ride along (the ADR's "진단 돌렸는데 근본원인 발견"
        # distinction lives in tool context), multimodal content is flattened.
        assert rec["body"][1]["tool_calls"] == ["terminal"]
        assert rec["body"][3]["content"] == "fixed it"

    def test_pre_compress_kind(self, mirror, tmp_path):
        mirror.append_messages("pre_compress", [{"role": "user", "content": "x"}])
        rec = _records(_month_file(tmp_path / "l0-mirror"))[0]
        assert rec["kind"] == "pre_compress"

    def test_non_dict_messages_are_skipped(self, mirror, tmp_path):
        mirror.append_messages("session_end", ["junk", {"role": "user", "content": "ok"}])
        rec = _records(_month_file(tmp_path / "l0-mirror"))[0]
        assert rec["meta"]["message_count"] == 1


class TestFailOpen:
    def test_append_never_raises_on_unwritable_dir(self, tmp_path):
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory")
        mirror = L0Mirror(base_dir=blocker / "l0-mirror")
        mirror.append_turn("s", "u", "a")  # must not raise
        mirror.append_messages("session_end", [{"role": "user", "content": "x"}])

    def test_kill_switch(self, mirror, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_MEMORY_JOURNAL_DISABLED", "1")
        mirror.append_turn("s", "u", "a")
        mirror.append_messages("session_end", [{"role": "user", "content": "x"}])
        assert not (tmp_path / "l0-mirror").exists()


# ---------------------------------------------------------------------------
# MemoryManager integration (stub ingest path)
# ---------------------------------------------------------------------------

class _StubIngestProvider(MemoryProvider):
    """Stub ingest path recording what state existed when ingest ran."""

    def __init__(self, mirror_file: Path):
        self._mirror_file = mirror_file
        self.synced: List[str] = []
        self.mirror_records_at_sync: List[int] = []

    @property
    def name(self) -> str:
        return "stub-ingest"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str = "", **kwargs) -> None:
        pass

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []

    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None):
        count = (
            len(_records(self._mirror_file)) if self._mirror_file.exists() else 0
        )
        self.mirror_records_at_sync.append(count)
        self.synced.append(user_content)

    def on_session_end(self, messages) -> None:
        self.mirror_records_at_sync.append(len(_records(self._mirror_file)))

    def on_pre_compress(self, messages) -> str:
        self.mirror_records_at_sync.append(len(_records(self._mirror_file)))
        return ""


def _manager(tmp_path, provider) -> MemoryManager:
    mm = MemoryManager()
    mm._pending_wal = None  # isolate: this file tests the mirror only
    mm._l0_mirror = L0Mirror(base_dir=tmp_path / "l0-mirror")
    mm.add_provider(provider)
    return mm


class TestManagerIntegration:
    def test_turn_mirrored_before_ingest_submit(self, tmp_path):
        mirror_file = _month_file(tmp_path / "l0-mirror")
        provider = _StubIngestProvider(mirror_file)
        mm = _manager(tmp_path, provider)

        mm.sync_all("hello", "world", session_id="sess-1")
        assert mm.flush_pending(timeout=5)

        assert provider.synced == ["hello"]
        # The mirror record already existed when the stub ingest ran.
        assert provider.mirror_records_at_sync == [1]
        rec = _records(mirror_file)[0]
        assert rec["kind"] == "sync_turn"
        assert rec["body"] == {"user": "hello", "assistant": "world"}
        assert rec["meta"]["providers"] == ["stub-ingest"]

    def test_session_end_and_pre_compress_mirrored(self, tmp_path):
        mirror_file = _month_file(tmp_path / "l0-mirror")
        provider = _StubIngestProvider(mirror_file)
        mm = _manager(tmp_path, provider)

        msgs = [{"role": "user", "content": "u"}, {"role": "assistant", "content": "a"}]
        mm.on_session_end(msgs)
        mm.on_pre_compress(msgs)

        kinds = [r["kind"] for r in _records(mirror_file)]
        assert kinds == ["session_end", "pre_compress"]
        # Each mirror record landed before its provider hook ran.
        assert provider.mirror_records_at_sync == [1, 2]

    def test_broken_mirror_never_blocks_sync(self, tmp_path):
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory")
        provider = _StubIngestProvider(blocker / "l0-mirror" / "never.jsonl")
        mm = MemoryManager()
        mm._pending_wal = None
        mm._l0_mirror = L0Mirror(base_dir=blocker / "l0-mirror")
        mm.add_provider(provider)

        mm.sync_all("hello", "world", session_id="sess-1")
        assert mm.flush_pending(timeout=5)
        assert provider.synced == ["hello"]
