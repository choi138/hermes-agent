"""Run the actual installer against two disposable homes through a real stdin transport."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

import pytest

import deploy
from deploy_support import checkout, fake_ssh

MANIFEST = ".local/state/review-gate/deploy-manifest.json"


class Installation:
    def __init__(self, tmp):
        self.root = checkout(tmp / "repo")
        self.repo = tmp / "repo"
        self.mac = tmp / "mac"
        self.server = tmp / "server home 'quoted"
        self.mac.mkdir()
        self.server.mkdir()
        self.log = tmp / "ssh.jsonl"
        self.env = dict(os.environ, HOME=str(self.mac), REVIEW_GATE_SSH=fake_ssh(
            tmp / "fake ssh.py", self.server, self.log))
        self.commit = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()

    def run(self, *args, code=0):
        result = subprocess.run(["/bin/sh", str(self.root / "install.sh"), *args],
                                env=self.env, capture_output=True, text=True, timeout=30, check=False)
        assert result.returncode == code, (result.stdout, result.stderr)
        return result

    def receipt(self, home):
        return json.loads((home / MANIFEST).read_text())

    def targets(self):
        return [Path(t["path"]) for home in (self.mac, self.server) for t in self.receipt(home)["targets"]]

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []


@pytest.fixture
def install(tmp_path):
    return Installation(tmp_path)


def snapshot(home):
    return {str(p.relative_to(home)): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode), p.stat().st_mtime_ns)
            for p in home.rglob("*") if p.is_file()}


def test_dry_run_and_check_write_nothing(install):
    result = install.run()
    assert len(result.stdout.splitlines()) == 8
    assert all(re.fullmatch(r"create  (mac|server):.+ repo=[0-9a-f]{12} deployed=missing", line)
               for line in result.stdout.splitlines())
    install.run("--check", code=1)
    assert list(install.mac.iterdir()) == list(install.server.iterdir()) == []
    assert len(install.calls()) == 2
    assert not list(install.repo.rglob("__pycache__"))


def test_apply_creates_modes_and_complete_manifests(install):
    result = install.run("--apply")
    assert "hermes hooks doctor will show mtime drift; hooks keep running." in result.stdout
    for home, host, prefix in ((install.mac, "mac", "M"), (install.server, "server", "S")):
        receipt = install.receipt(home)
        assert receipt["version"] == 1
        assert receipt["source_commit"] == install.commit
        assert receipt["host"] == host
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT.+Z", receipt["deployed_at"])
        assert {t["id"] for t in receipt["targets"]} == {prefix + str(i) for i in range(1, 5)}
        for target in receipt["targets"]:
            path = Path(target["path"])
            assert path.is_absolute() and path.is_relative_to(home)
            assert target["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
            assert stat.S_IMODE(path.stat().st_mode) == int(target["mode"], 8) == 0o755
        assert stat.S_IMODE((home / MANIFEST).stat().st_mode) == 0o600
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o700 for p in home.rglob("*") if p.is_dir())
    # Exactly one read and one apply over the fake transport, with SSH options intact.
    assert install.calls() == [["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "choi138-ri",
                               "/usr/bin/python3", "-"]] * 2
    expected = {str(p) for p in install.targets()} | {str(h / MANIFEST) for h in (install.mac, install.server)}
    actual = {str(p) for h in (install.mac, install.server) for p in h.rglob("*") if p.is_file()}
    assert actual == expected
    install.run("--check")


def test_identical_files_keep_mtime_but_manifests_are_written(install):
    install.run("--apply")
    for path in install.targets():
        os.utime(path, ns=(1000000000, 1000000000))
    before = {str(p): p.stat().st_mtime_ns for p in install.targets()}
    for home in (install.mac, install.server):
        (home / MANIFEST).unlink()
        (home / MANIFEST).parent.chmod(0o755)
    result = install.run("--apply")
    assert "mtime drift" not in result.stdout
    assert all(line.startswith("unchanged  ") for line in result.stdout.splitlines())
    assert {str(p): p.stat().st_mtime_ns for p in install.targets()} == before
    assert all(install.receipt(h)["source_commit"] == install.commit for h in (install.mac, install.server))
    assert all(stat.S_IMODE((h / MANIFEST).parent.stat().st_mode) == 0o700 for h in (install.mac, install.server))
    assert not list(install.mac.rglob("*.bak-*")) and not list(install.server.rglob("*.bak-*"))


def test_update_backup_preserves_bytes_mode_and_legacy_backup(install):
    install.run("--apply")
    target = install.mac / ".local/bin/magi"
    original = target.read_bytes()
    target.write_bytes(b"hand edit\n")
    target.chmod(0o700)
    os.utime(target, ns=(1000000000, 1000000000))
    legacy = target.with_name("magi.review-gate-backup")
    legacy.write_bytes(b"keep legacy\n")
    install.run("--check", code=1)
    result = install.run("--apply")
    assert "update  mac:" + str(target) in result.stdout
    backups = list(target.parent.glob("magi.bak-*"))
    assert len(backups) == 1 and re.fullmatch(r"magi.bak-\d{14}", backups[0].name)
    assert backups[0].read_bytes() == b"hand edit\n"
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o700
    assert backups[0].stat().st_mtime_ns == 1000000000
    assert target.read_bytes() == original and stat.S_IMODE(target.stat().st_mode) == 0o755
    assert legacy.read_bytes() == b"keep legacy\n"


def test_missing_and_mode_only_drift(install):
    install.run("--apply")
    target = install.server / ".hermes/scripts/guard_review_gate.py"
    target.unlink()
    assert "create  server:" + str(target) in install.run("--check", code=1).stdout
    install.run("--apply")
    target.chmod(0o600)
    assert "update  server:" + str(target) in install.run("--check", code=1).stdout
    assert "mtime drift" in install.run("--apply").stdout
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert not list(target.parent.glob("*.bak-*"))


@pytest.mark.parametrize("dirty", ["modified", "staged", "untracked"])
def test_dirty_ops_refuses_before_either_host_is_touched(install, dirty):
    if dirty == "untracked":
        (install.root / "new-file").write_text("untracked")
    else:
        path = install.root / "review_gate.py"
        path.write_bytes(path.read_bytes() + b"\n# modified\n")
        if dirty == "staged":
            subprocess.run(["git", "-C", str(install.repo), "add", "scripts/ops"], check=True)
    result = install.run("--apply", code=3)
    assert "dirty" in result.stderr
    assert list(install.mac.iterdir()) == list(install.server.iterdir()) == []
    assert not install.calls()


def test_no_git_repo_refuses_apply(install):
    # Move only this fixture's metadata out of the source tree.
    (install.repo / ".git").rename(install.repo.parent / "fixture-git")
    install.run("--apply", code=3)
    assert list(install.mac.iterdir()) == list(install.server.iterdir()) == []
    assert not install.calls()


@pytest.mark.parametrize("host,relative", [("mac", ".local/bin/review-gate"),
                                          ("server", ".hermes/scripts/guard_background_continue.py")])
def test_symlink_preflight_writes_nothing_on_either_host(install, tmp_path, host, relative):
    home = getattr(install, host)
    target = home / relative
    target.parent.mkdir(parents=True)
    outside = tmp_path / "do-not-touch"
    outside.write_text("preserve")
    target.symlink_to(outside)
    before = (snapshot(install.mac), snapshot(install.server))
    assert "symlink" in install.run("--apply", code=3).stderr
    assert (snapshot(install.mac), snapshot(install.server)) == before
    assert outside.read_text() == "preserve"


@pytest.mark.parametrize("host,relative", [("mac", ".local/bin"), ("server", ".hermes/scripts")])
def test_symlinked_parent_below_home_writes_nothing(install, tmp_path, host, relative):
    home = getattr(install, host)
    link = home / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "redirected"
    outside.mkdir()
    link.symlink_to(outside, target_is_directory=True)
    before = (snapshot(install.mac), snapshot(install.server))
    assert "destination parent" in install.run("--apply", code=3).stderr
    assert (snapshot(install.mac), snapshot(install.server)) == before
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("host", ["mac", "server"])
def test_host_selection(install, host):
    install.run("--apply", "--host", host, "--server-host", "fake-host")
    selected = getattr(install, host)
    other = install.server if host == "mac" else install.mac
    assert len(install.receipt(selected)["targets"]) == 4
    assert list(other.iterdir()) == []
    assert len(install.calls()) == (2 if host == "server" else 0)
    if host == "server":
        assert all(call[-3] == "fake-host" for call in install.calls())


@pytest.mark.parametrize("args", [("--apply", "--check"), ("--host", "invalid"), ("--unknown",)])
def test_usage_errors_are_three(install, args):
    install.run(*args, code=3)
    assert not install.calls()


def test_ssh_failure_is_three_and_does_not_write_mac(install):
    install.env["REVIEW_GATE_SSH"] = "/usr/bin/false"
    install.run("--check", code=3)
    install.run("--apply", code=3)
    assert list(install.mac.iterdir()) == list(install.server.iterdir()) == []


def test_default_rendered_bytes(monkeypatch):
    monkeypatch.setenv("HOME", "/Users/choegeun-won")
    args = deploy.parser().parse_args([])
    fixture = Path(__file__).parent / "fixtures/magi-default.sh"
    assert deploy.render(deploy.SOURCE / "magi-shim.sh", args) == fixture.read_bytes()
    wrapper = deploy.render(deploy.SOURCE / "review-gate-wrapper.sh", args)
    assert wrapper == (b'#!/bin/sh\nexec /usr/bin/python3 /Users/choegeun-won/.local/share/'
                       b'review-gate/review_gate.py "$@"\n')
    assert len(wrapper) == 97
    weekly = deploy.raw(deploy.SOURCE / "audit/review_audit_weekly.sh", args)
    assert weekly == (b'#!/bin/sh\n# Weekly review-gate audit (Korean plain text). Delivered verbatim by Hermes cron (no_agent).\n'
                      b'exec timeout 300 /usr/bin/python3 /home/justin/.hermes/scripts/review_audit_report.py\n')
    assert len(weekly) == 190


@pytest.mark.parametrize("symlink", [False, True])
def test_backup_collision_never_overwrites_existing_file(tmp_path, monkeypatch, symlink):
    from datetime import datetime
    import deploy_io

    class Frozen:
        @staticmethod
        def now(zone):
            return datetime(2026, 10, 20, 12, tzinfo=zone)

    monkeypatch.setattr(deploy_io, "datetime", Frozen)
    path = tmp_path / "target"
    path.write_bytes(b"old target")
    backup = path.with_name("target.bak-20261020120000")
    if symlink:
        backup.symlink_to(tmp_path / "absent")
    else:
        backup.write_bytes(b"earlier backup")
    with pytest.raises(FileExistsError):
        deploy_io.backup(path)
    assert path.read_bytes() == b"old target"
    assert backup.is_symlink() if symlink else backup.read_bytes() == b"earlier backup"


def test_failed_atomic_replace_preserves_original_and_removes_temp(tmp_path, monkeypatch):
    import deploy_io

    destination = tmp_path / "destination"
    destination.mkdir()
    path = destination / "target"
    path.write_bytes(b"original")
    def fail_replace(source, destination):
        assert Path(source).parent == path.parent
        assert Path(source).read_bytes() == b"replacement"
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o755
        raise OSError("replace failed")
    monkeypatch.setattr(deploy_io.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        deploy_io.atomic_write(path, b"replacement", 0o755)
    assert path.read_bytes() == b"original"
    assert list(destination.iterdir()) == [path]
