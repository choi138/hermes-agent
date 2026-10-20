"""Manifest drift is detected by deployed, standalone audit scripts without a checkout."""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from unittest.mock import patch

import pytest

from audit import review_audit_mac as mac
from audit import review_audit_report as report

MANIFEST = ".local/state/review-gate/deploy-manifest.json"


def receipt(home, host):
    home.mkdir(parents=True, exist_ok=True)
    targets = []
    for index in range(1, 5):
        path = home / ("target-" + str(index))
        path.write_bytes(b"last install\n")
        path.chmod(0o755)
        targets.append(dict(id=host[0].upper() + str(index), path=str(path),
                            sha256=hashlib.sha256(path.read_bytes()).hexdigest(), mode="0755"))
    result = dict(version=1, source_commit=("a" if host == "mac" else "b") * 40,
                  deployed_at="2026-10-20T00:00:00Z", host=host, targets=targets)
    manifest = home / MANIFEST
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps(result))
    return result


@pytest.mark.parametrize("host,module", [("mac", mac), ("server", report)])
def test_manifest_target_states(tmp_path, monkeypatch, host, module):
    home = tmp_path / host
    raw = receipt(home, host)
    monkeypatch.setenv("HOME", str(home))
    state = module.deploy_state(host)
    assert state["manifest"] == "ok"
    assert state["source_commit"] == raw["source_commit"] and state["deployed_at"] == raw["deployed_at"]
    assert [t["status"] for t in state["targets"]] == ["ok"] * 4
    Path(raw["targets"][0]["path"]).write_text("hand edit")
    Path(raw["targets"][1]["path"]).chmod(0o644)
    Path(raw["targets"][2]["path"]).unlink()
    assert [t["status"] for t in module.deploy_state(host)["targets"]] == ["modified", "modified", "missing", "ok"]
    (home / MANIFEST).unlink()
    assert module.deploy_state(host) == dict(manifest="missing", source_commit=None, deployed_at=None, targets=[])


@pytest.mark.parametrize("invalid", ["{bad", "[]", '{}', '{"version":2}'])
def test_invalid_manifest(tmp_path, monkeypatch, invalid):
    receipt(tmp_path, "mac")
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / MANIFEST).write_text(invalid)
    assert mac.deploy_state("mac")["manifest"] == "invalid"


def test_incomplete_manifest_is_invalid(tmp_path, monkeypatch):
    raw = receipt(tmp_path, "mac")
    raw["targets"].pop()
    (tmp_path / MANIFEST).write_text(json.dumps(raw))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert mac.deploy_state("mac")["manifest"] == "invalid"


def test_mac_json_includes_deploy_and_preserves_audit_data(tmp_path, monkeypatch):
    raw = receipt(tmp_path, "mac")
    monkeypatch.setenv("HOME", str(tmp_path))
    output = io.StringIO()
    with patch.object(mac, "load_ledger", return_value={"runs": []}), redirect_stdout(output):
        assert mac.main(["--since", "2026-10-13", "--json"]) == 0
    data = json.loads(output.getvalue())
    assert data["deploy"]["source_commit"] == raw["source_commit"]
    assert all(t["status"] == "ok" for t in data["deploy"]["targets"])
    assert data["since"] == "2026-10-13" and "ledger" in data and "sessions" in data


@pytest.fixture
def both(tmp_path, monkeypatch):
    server = tmp_path / "server"
    raw_server = receipt(server, "server")
    mac_home = tmp_path / "mac"
    raw_mac = receipt(mac_home, "mac")
    monkeypatch.setenv("HOME", str(mac_home))
    mac_state = mac.deploy_state("mac")
    monkeypatch.setenv("HOME", str(server))
    return raw_server, raw_mac, dict(ledger={}, sessions={"unledgered": []}, deploy=mac_state)


def render(data):
    return report.format_report("2026-10-13", [], data)


def test_report_all_ok(both):
    _, _, data = both
    output = render(data)
    assert "배포 드리프트\n배포본 = 마지막 설치본 (mac aaaaaaa, server bbbbbbb)" in output
    assert "원본:" not in output


def test_report_modified_server(both):
    server, _, data = both
    path = Path(server["targets"][0]["path"])
    path.write_text("manual edit")
    output = render(data)
    assert f"server S1 {path}: 수정됨" in output
    assert "원본: hermes/all-work scripts/ops → install.sh --check 후 --apply" in output


def test_report_missing_mac_file(both):
    _, raw_mac, data = both
    target = data["deploy"]["targets"][0]
    # Use the real collector result sent back from the Mac.
    Path(target["path"]).unlink()
    with patch.dict(os.environ, HOME=str(Path(raw_mac["targets"][0]["path"]).parent)):
        data["deploy"] = mac.deploy_state("mac")
    assert "mac M1 {}: 없음".format(target["path"]) in render(data)


def test_report_missing_and_invalid_manifest(both):
    _, _, data = both
    path = Path.home() / MANIFEST
    path.unlink()
    assert "server 설치 기록 없음" in render(data)
    path.write_text("{bad")
    assert "server 설치 기록 없음" in render(data)
    data["deploy"] = dict(manifest="invalid")
    assert "mac 설치 기록 없음" in render(data)


def test_mac_unavailable_still_checks_server_and_exits_zero(both):
    server, _, _ = both
    path = Path(server["targets"][2]["path"])
    path.write_text("drift")
    output = io.StringIO()
    with patch.object(report, "mac_json", side_effect=subprocess.CalledProcessError(255, ["ssh"])), redirect_stdout(output):
        assert report.main(["--since", "2026-10-13"]) == 0
    assert "mac 확인 불가" in output.getvalue()
    assert f"server S3 {path}: 수정됨" in output.getvalue()


def test_deployed_audits_work_without_repo(both, tmp_path, monkeypatch):
    _, raw_mac, data = both
    # Copy the deliverables, then run from an empty directory without source imports.
    import shutil
    import deploy
    out = tmp_path / "standalone"
    out.mkdir()
    for filename in ("review_audit_mac.py", "review_audit_report.py"):
        shutil.copy2(deploy.SOURCE / "audit" / filename, out / filename)
    gate = out / "ledger.py"
    gate.write_text('print(\'{"runs": [], "events": []}\')\n')
    mac_home = Path(raw_mac["targets"][0]["path"]).parent
    env = dict(os.environ, HOME=str(mac_home), REVIEW_GATE_PY=str(gate),
               CODEX_SESSIONS_DIR=str(out / "no-sessions"), CLAUDE_PROJECTS_DIR=str(out / "no-projects"))
    result = subprocess.run([sys.executable, str(out / "review_audit_mac.py"), "--since", "2026-10-13", "--json"],
                            cwd=out, env=env, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["deploy"] == data["deploy"]
    fake_mac = out / "mac.py"
    fake_mac.write_text(f"print({result.stdout!r})\n")
    monkeypatch.setenv("REVIEW_AUDIT_MAC_CMD", shlex.join([sys.executable, str(fake_mac)]))
    result = subprocess.run([sys.executable, str(out / "review_audit_report.py")], cwd=out,
                            capture_output=True, text=True, check=True)
    assert "배포본 = 마지막 설치본 (mac aaaaaaa, server bbbbbbb)" in result.stdout
