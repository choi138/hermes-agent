"""Tests for the L0-mirror — the local evidence journal (ADR-004 §② L0-mirror).

Every per-turn sync payload that leaves for external memory is appended (full
scrubbed content) to a monthly JSONL under ``memory/l0-mirror/`` just before
ingest submit, so a total graph loss can be rebuilt from local disk. Boundary
extraction inputs (session_end / pre_compress) re-send content the per-turn
records already hold, so they are journaled as content-free MARKER records
(role sequence + tool-call names) instead of re-mirroring the transcript on
every compaction. Scrubbed, fail-open, zero LLM calls.

Covers: the record shape for both payload kinds, monthly file naming, the
scrub, the fail-open contract, and the MemoryManager stub-ingest integration
(per-turn mirror written before provider dispatch; boundary markers appended
via the background worker; broken mirror never blocks sync).
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


class TestBoundaryMarkers:
    def test_session_end_marker_with_tool_calls_and_no_content(self, mirror, tmp_path):
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
        roles = [m["role"] for m in rec["skeleton"]]
        assert roles == ["user", "assistant", "tool", "assistant"]
        # Tool-call names ride along (the ADR's "진단 돌렸는데 근본원인 발견"
        # distinction lives in tool context) …
        assert rec["skeleton"][1]["tool_calls"] == ["terminal"]
        # … but message CONTENT does not: per-turn records already mirror it,
        # and re-mirroring the transcript at every boundary made the journal
        # grow superlinearly under repeated compaction. Only sizes survive
        # (multimodal content flattened before measuring).
        raw = _month_file(tmp_path / "l0-mirror").read_text(encoding="utf-8")
        assert "diagnose the failure" not in raw
        assert "fixed it" not in raw
        assert rec["skeleton"][0]["chars"] == len("diagnose the failure")
        assert rec["skeleton"][3]["chars"] == len("fixed it")

    def test_pre_compress_kind(self, mirror, tmp_path):
        mirror.append_messages("pre_compress", [{"role": "user", "content": "x"}])
        rec = _records(_month_file(tmp_path / "l0-mirror"))[0]
        assert rec["kind"] == "pre_compress"

    def test_non_dict_messages_are_skipped(self, mirror, tmp_path):
        mirror.append_messages("session_end", ["junk", {"role": "user", "content": "ok"}])
        rec = _records(_month_file(tmp_path / "l0-mirror"))[0]
        assert rec["meta"]["message_count"] == 1

    def test_build_boundary_record_is_none_when_disabled(self, mirror, monkeypatch):
        monkeypatch.setenv("HERMES_MEMORY_JOURNAL_DISABLED", "1")
        assert mirror.build_boundary_record("session_end", [{"role": "user", "content": "x"}]) is None


class TestConcurrentAppend:
    def test_parallel_large_appends_to_one_file_never_corrupt_lines(self, tmp_path):
        """Gateway reality: many managers' mem-sync workers — plus boundary
        callers — append to the SAME monthly file. Each record is written as
        one os.write on an O_APPEND fd under a per-path lock; records larger
        than the old 8KB io buffer used to split across raw writes and
        interleave into corrupt (silently dropped) lines."""
        import threading

        base = tmp_path / "l0-mirror"
        big = "lorem " * 12000  # ~72KB, far past any io buffer size
        n_threads, n_appends = 8, 5

        def worker(idx: int) -> None:
            mirror = L0Mirror(base_dir=base)  # one instance per "manager"
            for i in range(n_appends):
                mirror.append_turn(f"sess-{idx}", f"u-{idx}-{i} {big}", "a")

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = _month_file(base).read_text(encoding="utf-8").splitlines()
        recs = [json.loads(line) for line in lines if line.strip()]  # ALL must parse
        assert len(recs) == n_threads * n_appends
        seen = {r["body"]["user"].split(" ")[0] for r in recs}
        assert len(seen) == n_threads * n_appends  # no record lost or merged

    def test_concurrent_appends_after_truncated_tail_heal_once(self, tmp_path):
        """The tail-heal check-then-act must be serialized: exactly one heal
        newline, every post-crash record intact."""
        import threading

        base = tmp_path / "l0-mirror"
        mirror = L0Mirror(base_dir=base)
        mirror.append_turn("s", "before-crash", "a")
        path = _month_file(base)
        with open(path, "ab") as f:
            f.write(b'{"ts": 1, "kind": "sync_turn", "trunc')  # crash mid-append

        threads = [
            threading.Thread(target=mirror.append_turn, args=("s", f"after-{i}", "a"))
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = path.read_text(encoding="utf-8").splitlines()
        parsed, corrupt = [], []
        for line in lines:
            try:
                parsed.append(json.loads(line))
            except Exception:
                corrupt.append(line)
        assert len(corrupt) == 1  # only the original crash fragment
        users = {r["body"]["user"] for r in parsed}
        assert users == {"before-crash"} | {f"after-{i}" for i in range(8)}


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
        count = (
            len(_records(self._mirror_file)) if self._mirror_file.exists() else 0
        )
        self.mirror_records_at_sync.append(count)

    def on_pre_compress(self, messages) -> str:
        count = (
            len(_records(self._mirror_file)) if self._mirror_file.exists() else 0
        )
        self.mirror_records_at_sync.append(count)
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

    def test_session_end_and_pre_compress_write_boundary_markers(self, tmp_path):
        mirror_file = _month_file(tmp_path / "l0-mirror")
        provider = _StubIngestProvider(mirror_file)
        mm = _manager(tmp_path, provider)

        msgs = [{"role": "user", "content": "u"}, {"role": "assistant", "content": "a"}]
        mm.on_session_end(msgs)
        mm.on_pre_compress(msgs)
        # Boundary markers are appended on the background worker — the
        # calling thread (on_pre_compress fires MID-TURN inside
        # compress_context) never pays journal disk I/O inline.
        assert mm.flush_pending(timeout=5)

        recs = _records(mirror_file)
        assert [r["kind"] for r in recs] == ["session_end", "pre_compress"]
        # Content-free markers: the transcript is per-turn-mirrored already.
        for rec in recs:
            assert [m["role"] for m in rec["skeleton"]] == ["user", "assistant"]
            assert rec["meta"]["message_count"] == 2
            assert all("content" not in m for m in rec["skeleton"])
        # Provider hooks themselves ran synchronously, both before markers
        # could be counted on (worker ordering is not part of the contract).
        assert len(provider.mirror_records_at_sync) == 2

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
