"""Tests for write_file post-write content verification (verified flag)."""

import json
import os
from unittest.mock import patch as mock_patch

import pytest

from tools.file_tools import write_file_tool


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    return tmp_path


class TestWriteVerification:
    def test_successful_write_reports_verified(self, workdir):
        f = workdir / "out.txt"
        r = json.loads(write_file_tool(str(f), "hello verified world\n", task_id="t-wv"))
        assert r.get("bytes_written") == len("hello verified world\n")
        assert r.get("verified") is True

    def test_unicode_content_verified(self, workdir):
        f = workdir / "uni.txt"
        content = "línea → uno · ✓\n"
        r = json.loads(write_file_tool(str(f), content, task_id="t-wv"))
        assert r.get("verified") is True

    def test_crlf_preservation_still_verifies(self, workdir):
        # Existing CRLF file: write_file converts LF content to CRLF before
        # writing; verification hashes the shim-adjusted content, so it must
        # still report verified.
        f = workdir / "win.txt"
        f.write_bytes(b"old line\r\n")
        r = json.loads(write_file_tool(str(f), "new line\nsecond\n", task_id="t-wv"))
        assert "error" not in r
        assert r.get("verified") is True
        assert b"\r\n" in f.read_bytes()

    def test_hash_mismatch_is_hard_error(self, workdir):
        f = workdir / "bad.txt"
        import tools.file_operations as fo
        real_sha = fo.hashlib.sha256

        class _WrongHash:
            def __init__(self, *a, **k):
                self._h = real_sha(b"different content entirely")
            def hexdigest(self):
                return self._h.hexdigest()

        with mock_patch.object(fo.hashlib, "sha256", _WrongHash):
            r = json.loads(write_file_tool(str(f), "actual content\n", task_id="t-wv"))
        assert "error" in r
        assert "did not persist" in r["error"]

    def test_verification_failure_never_breaks_write(self, workdir):
        # sha256sum unavailable/failing -> verified omitted, write still ok.
        f = workdir / "ok.txt"
        import tools.file_operations as fo

        real_exec = fo.ShellFileOperations._exec

        def flaky_exec(self, cmd, **kw):
            if "sha256sum" in cmd:
                raise RuntimeError("no hash binary")
            return real_exec(self, cmd, **kw)

        with mock_patch.object(fo.ShellFileOperations, "_exec", flaky_exec):
            r = json.loads(write_file_tool(str(f), "content lands anyway\n", task_id="t-wv2"))
        assert "error" not in r
        assert f.read_text() == "content lands anyway\n"
        assert "verified" not in r or r.get("verified") is None


class TestBytesWrittenIsTheResultingFileSize:
    """R4 step 2: write_file's schema now tells the model that bytes_written
    on a SUCCESSFUL write is the resulting file's size, so it can report a
    byte count without spending a model round on a follow-up wc/ls/stat.

    The success scoping is load-bearing, not decorative: WriteResult.
    bytes_written defaults to 0 and to_dict only filters ``v is not None``,
    so every error-shaped result carries ``bytes_written: 0`` for a file that
    still holds its previous content.  An unscoped claim would license
    reporting "0 bytes" for a failed write.
    """

    def test_schema_documents_bytes_written_and_scopes_it_to_success(self):
        from tools.file_tools import WRITE_FILE_SCHEMA

        description = WRITE_FILE_SCHEMA["description"]
        assert "bytes_written" in description
        assert "successful write" in description

    def test_bytes_written_equals_on_disk_size_for_a_fresh_file(self, workdir):
        f = workdir / "fresh.txt"
        content = "日本語のロレムイプサム\n"
        r = json.loads(write_file_tool(str(f), content, task_id="t-bw"))
        assert "error" not in r
        assert r["bytes_written"] == os.stat(f).st_size
        # Multibyte: a character count would have been wrong.
        assert r["bytes_written"] > len(content)

    def test_bytes_written_is_file_size_not_payload_size_on_crlf_overwrite(
        self, workdir
    ):
        """The case that makes ``len(content.encode())`` the wrong assertion.

        Overwriting an existing CRLF file re-normalizes LF to CRLF before
        writing, so the resulting file is strictly larger than the payload
        the caller handed in.  bytes_written comes from ``wc -c`` on the
        result, so it must track the FILE, not the argument.
        """
        f = workdir / "crlf.txt"
        f.write_bytes(b"old\r\n")
        content = "alpha\nbeta\ngamma\n"
        r = json.loads(write_file_tool(str(f), content, task_id="t-bw2"))
        assert "error" not in r
        assert b"\r\n" in f.read_bytes()
        assert r["bytes_written"] == os.stat(f).st_size
        assert r["bytes_written"] > len(content.encode("utf-8"))

    def test_error_shaped_result_still_carries_a_zero_byte_count(self, workdir):
        """Why the description says "on a successful write (no error field)".

        A post-write hash mismatch aborts with an error, but the serialized
        result still contains ``bytes_written: 0``.  Reporting that as the
        file's size would be a lie — the file is untouched or stale.  This
        pins the hazard the wording guards against, so a future edit that
        drops the scoping fails here.
        """
        f = workdir / "mismatch.txt"
        f.write_text("previous content\n")
        import tools.file_operations as fo

        real_sha = fo.hashlib.sha256

        class _WrongHash:
            def __init__(self, *a, **k):
                self._h = real_sha(b"not what was written")

            def hexdigest(self):
                return self._h.hexdigest()

        with mock_patch.object(fo.hashlib, "sha256", _WrongHash):
            r = json.loads(write_file_tool(str(f), "new content\n", task_id="t-bw3"))

        assert "error" in r
        assert r.get("bytes_written") == 0
        assert os.stat(f).st_size != 0
