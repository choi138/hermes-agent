"""Remote bytecode must not break write-back into read-only installed skills."""

import io
import json
import subprocess
import sys
import tarfile

import pytest

from tools.environments import file_sync, ssh_sync_back


def test_generated_bytecode_is_not_applied_to_read_only_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(file_sync, "_credential_host_paths", set)
    scripts = tmp_path / "skill" / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "run.py"
    script.write_text("print('hello')")
    result = tmp_path / "result.md"
    result.write_text("old")
    mapping = [(str(script), "/remote/skill/scripts/run.py"), (str(result), "/remote/result.md")]

    def download(dest):
        with tarfile.open(dest, "w") as archive:
            for name, content in [("remote/skill/scripts/__pycache__/run.cpython.pyc", b"bytecode"),
                                  ("remote/result.md", b"completed result")]:
                member = tarfile.TarInfo(name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))

    manager = file_sync.FileSyncManager(lambda: mapping, lambda *args: None, lambda _: None,
                                       bulk_download_fn=download)
    manager._pushed_hashes = {remote: file_sync._sha256_file(host) for host, remote in mapping}
    monkeypatch.setattr(file_sync, "_sleep", lambda _: pytest.fail("permanent failure retried"))
    scripts.chmod(0o555)
    try:
        manager.sync_back()
        assert result.read_text() == "completed result"
        assert not (scripts / "__pycache__").exists()
        assert script.read_text() == "print('hello')"
    finally:
        scripts.chmod(0o755)


def test_local_permission_failure_is_not_retried(tmp_path, monkeypatch):
    manager = file_sync.FileSyncManager(list, lambda *args: None, lambda _: None,
                                       bulk_download_fn=lambda _: None)
    manager._pushed_hashes = {"/remote/result": "baseline"}
    attempts = []

    def denied(_):
        attempts.append(True)
        raise PermissionError("read-only installed skill")

    monkeypatch.setattr(manager, "_sync_back_once", denied)
    monkeypatch.setattr(file_sync, "_sleep", lambda _: None)
    manager.sync_back()
    assert attempts == [True]


@pytest.mark.platforms("posix")
def test_selective_download_prunes_unreadable_bytecode_tree(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "result.md").write_text("result")
    bytecode = remote / "__pycache__"
    bytecode.mkdir()
    (bytecode / "run.pyc").write_bytes(b"generated")
    request = {"roots": [str(remote)], "excluded": [], "hashes": {},
               "max_bytes": 65536, "timeout": 5}
    bytecode.chmod(0)
    try:
        response = subprocess.run([sys.executable, "-c", ssh_sync_back._REMOTE_PROGRAM],
                                  input=json.dumps(request).encode(),
                                  capture_output=True, timeout=8, check=False)
    finally:
        bytecode.chmod(0o755)
    assert response.returncode == 0, response.stderr
    with tarfile.open(fileobj=io.BytesIO(response.stdout)) as archive:
        assert archive.getnames() == [str(remote / "result.md").lstrip("/")]
