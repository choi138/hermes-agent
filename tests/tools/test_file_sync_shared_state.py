"""Shared upload state: a fresh SSH manager must not re-upload what the remote already has."""

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hermes_constants import get_hermes_home
from tools.environments.file_sync import _SHARED_STATE_VERSION, FileSyncManager

IDENTITY = json.dumps(["mac", "me", 22, "/Users/me"])


class FakeRemote:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.uploads: list[str] = []
        self.deletes: list[str] = []
        self.verify_calls: list[list[str]] = []
        self.verify_error: Exception | None = None

    def upload(self, host_path, remote_path):
        self.files[remote_path] = Path(host_path).read_bytes()
        self.uploads.append(remote_path)

    def delete(self, remote_paths):
        for path in remote_paths:
            self.files.pop(path, None)
            self.deletes.append(path)

    def verify(self, remote_paths, timeout):
        self.verify_calls.append(list(remote_paths))
        if self.verify_error is not None:
            raise self.verify_error
        return {path: hashlib.sha256(self.files[path]).hexdigest() if path in self.files else None
                for path in remote_paths}


@pytest.fixture
def host_files(tmp_path):
    files = {}
    for name in ("a.py", "b.py", "c.json"):
        path = tmp_path / name
        path.write_text(f"content of {name}", encoding="utf-8")
        files[name] = str(path)
    return files


def _manager(remote, files, *, identity=IDENTITY, verify=True):
    mapping = [(host, f"/Users/me/.hermes/{name}") for name, host in files.items()]
    return FileSyncManager(
        get_files_fn=lambda: [(h, r) for h, r in mapping if Path(h).exists()],
        upload_fn=remote.upload, delete_fn=remote.delete,
        selective_download_fn=MagicMock(), sync_back_identity=identity, fail_closed=True,
        verify_remote_fn=remote.verify if verify else None)


def _state_files():
    directory = get_hermes_home() / "sync-state"
    return sorted(directory.glob("*.json")) if directory.exists() else []


def test_fresh_manager_reuses_files_already_on_remote(host_files):
    remote = FakeRemote()
    _manager(remote, host_files).sync(force=True)
    assert len(remote.uploads) == 3

    remote.uploads.clear()
    fresh = _manager(remote, host_files)
    fresh.sync(force=True)

    assert remote.uploads == []
    assert len(remote.verify_calls) == 1
    assert sorted(fresh._pushed_hashes) == sorted(remote.files)

    fresh.sync(force=True)
    assert remote.uploads == [] and len(remote.verify_calls) == 1


def test_changed_or_missing_remote_copies_are_uploaded(host_files):
    remote = FakeRemote()
    _manager(remote, host_files).sync(force=True)
    remote.files["/Users/me/.hermes/a.py"] = b"edited on the remote"
    del remote.files["/Users/me/.hermes/b.py"]

    remote.uploads.clear()
    _manager(remote, host_files).sync(force=True)

    assert sorted(remote.uploads) == ["/Users/me/.hermes/a.py", "/Users/me/.hermes/b.py"]
    assert remote.files["/Users/me/.hermes/a.py"] == b"content of a.py"


def test_host_change_since_shared_upload_is_uploaded(host_files):
    remote = FakeRemote()
    _manager(remote, host_files).sync(force=True)
    path = Path(host_files["c.json"])
    path.write_text("a longer rewritten host file", encoding="utf-8")
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))

    remote.uploads.clear()
    _manager(remote, host_files).sync(force=True)

    assert remote.uploads == ["/Users/me/.hermes/c.json"]
    assert "/Users/me/.hermes/c.json" not in remote.verify_calls[0]


def test_remote_check_failure_falls_back_to_full_upload(host_files):
    remote = FakeRemote()
    _manager(remote, host_files).sync(force=True)
    remote.verify_error = TimeoutError("ssh timed out")

    remote.uploads.clear()
    _manager(remote, host_files).sync(force=True)

    assert len(remote.uploads) == 3


@pytest.mark.parametrize("content", [
    "not json",
    json.dumps({"version": 1, "identity": IDENTITY, "files": {}}),
    json.dumps({"version": _SHARED_STATE_VERSION, "identity": "other", "files": {}}),
    json.dumps({"version": _SHARED_STATE_VERSION, "identity": IDENTITY, "files": {"x": 1}}),
    json.dumps({"version": _SHARED_STATE_VERSION, "identity": IDENTITY, "files": {"x": {"host": "h"}}}),
])
def test_unusable_shared_state_is_ignored(host_files, content):
    remote = FakeRemote()
    _manager(remote, host_files).sync(force=True)
    for path in _state_files():
        path.write_text(content, encoding="utf-8")

    remote.uploads.clear()
    _manager(remote, host_files).sync(force=True)

    assert len(remote.uploads) == 3


def test_other_remote_identity_does_not_share_state(host_files):
    remote = FakeRemote()
    _manager(remote, host_files).sync(force=True)

    remote.uploads.clear()
    _manager(remote, host_files, identity=json.dumps(["other-mac", "me", 22, "/Users/me"])).sync(force=True)

    assert len(remote.uploads) == 3
    assert len(_state_files()) == 2


def test_deletions_stay_per_manager(host_files):
    remote = FakeRemote()
    owner = _manager(remote, host_files)
    owner.sync(force=True)

    narrower = {name: host for name, host in host_files.items() if name != "c.json"}
    _manager(remote, narrower).sync(force=True)
    assert remote.deletes == []
    assert "/Users/me/.hermes/c.json" in remote.files

    Path(host_files["c.json"]).unlink()
    owner.sync(force=True)
    assert remote.deletes == ["/Users/me/.hermes/c.json"]
    shared = json.loads(_state_files()[0].read_text(encoding="utf-8"))
    assert "/Users/me/.hermes/c.json" not in shared["files"]


def test_live_managers_do_not_duplicate_each_others_uploads(host_files, tmp_path):
    remote = FakeRemote()
    first = _manager(remote, host_files)
    second = _manager(remote, host_files)
    first.sync(force=True)
    second.sync(force=True)
    assert len(remote.uploads) == 3

    new_file = tmp_path / "d.md"
    new_file.write_text("new skill", encoding="utf-8")
    with_new = dict(host_files, **{"d.md": str(new_file)})
    first._get_files_fn = _manager(remote, with_new)._get_files_fn
    second._get_files_fn = first._get_files_fn
    first.sync(force=True)
    second.sync(force=True)

    assert remote.uploads.count("/Users/me/.hermes/d.md") == 1


def test_manager_without_remote_check_keeps_no_shared_state(host_files):
    remote = FakeRemote()
    _manager(remote, host_files, verify=False).sync(force=True)
    _manager(remote, host_files, verify=False).sync(force=True)

    assert len(remote.uploads) == 6
    assert _state_files() == []


def test_same_remote_path_from_other_host_source_is_uploaded(tmp_path):
    first_dir, second_dir = tmp_path / "project_a", tmp_path / "project_b"
    first_dir.mkdir()
    second_dir.mkdir()
    first, second = first_dir / "SKILL.md", second_dir / "SKILL.md"
    first.write_text("instructions A", encoding="utf-8")
    second.write_text("instructions B", encoding="utf-8")
    stamp = first.stat().st_mtime
    os.utime(second, (stamp, stamp))

    remote = FakeRemote()
    _manager(remote, {"SKILL.md": str(first)}).sync(force=True)
    remote.uploads.clear()
    _manager(remote, {"SKILL.md": str(second)}).sync(force=True)

    assert remote.uploads == ["/Users/me/.hermes/SKILL.md"]
    assert remote.files["/Users/me/.hermes/SKILL.md"] == b"instructions B"


def test_host_permission_change_is_uploaded(host_files):
    remote = FakeRemote()
    _manager(remote, host_files).sync(force=True)
    path = Path(host_files["a.py"])
    stat = path.stat()
    path.chmod(0o755)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    remote.uploads.clear()
    _manager(remote, host_files).sync(force=True)

    assert remote.uploads == ["/Users/me/.hermes/a.py"]


def test_entries_for_removed_host_files_are_pruned(host_files, tmp_path):
    remote = FakeRemote()
    _manager(remote, host_files).sync(force=True)
    Path(host_files["c.json"]).unlink()

    new_file = tmp_path / "d.md"
    new_file.write_text("new skill", encoding="utf-8")
    _manager(remote, {"d.md": str(new_file)}).sync(force=True)

    shared = json.loads(_state_files()[0].read_text(encoding="utf-8"))
    assert sorted(shared["files"]) == ["/Users/me/.hermes/a.py", "/Users/me/.hermes/b.py", "/Users/me/.hermes/d.md"]


def test_timestamp_preserving_replace_is_uploaded(host_files):
    remote = FakeRemote()
    _manager(remote, host_files).sync(force=True)
    path = Path(host_files["a.py"])
    stat = path.stat()
    path.write_text("content of b.py", encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    remote.uploads.clear()
    _manager(remote, host_files).sync(force=True)

    assert remote.uploads == ["/Users/me/.hermes/a.py"]
    assert remote.files["/Users/me/.hermes/a.py"] == b"content of b.py"


def test_racy_upload_is_adopted_after_its_key_settles(host_files):
    remote = FakeRemote()
    _manager(remote, host_files).sync(force=True)
    for host in host_files.values():
        stat = os.stat(host)
        os.utime(host, ns=(stat.st_atime_ns, stat.st_mtime_ns - 60 * 10**9))

    remote.uploads.clear()
    fresh = _manager(remote, host_files)
    fresh.sync(force=True)

    assert remote.uploads == []
    stored = json.loads(_state_files()[0].read_text())["files"]
    assert all(entry["key"][0] >= 0 for entry in stored.values())
