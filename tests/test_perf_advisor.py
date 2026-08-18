"""Tests for the slow-tool perf advisor (tools/perf_advisor.py)."""

import pytest

from tools.perf_advisor import perf_advisory


RGLOB_CMD = (
    "python3 - <<'PY'\nfrom pathlib import Path\n"
    "for p in Path('/home/ubuntu/apifuse').rglob('*'):\n    pass\nPY"
)


def _no_env(monkeypatch):
    for var in (
        "HERMES_PERF_ADVISOR",
        "HERMES_PERF_ADVISOR_MIN_S",
        "HERMES_PERF_ADVISOR_FOREGROUND_S",
    ):
        monkeypatch.delenv(var, raising=False)


# ── tree-scan advisories ────────────────────────────────────────────


def test_rglob_slow_fires(monkeypatch):
    _no_env(monkeypatch)
    hint = perf_advisory("terminal", {"command": RGLOB_CMD}, 88.0)
    assert hint is not None
    assert "search_files" in hint
    assert hint.startswith("\n\n[perf-advisor]")


def test_os_walk_slow_fires(monkeypatch):
    _no_env(monkeypatch)
    cmd = "python3 -c 'import os\nfor r,d,f in os.walk(\"/home/ubuntu\"): pass'"
    assert perf_advisory("terminal", {"command": cmd}, 30.0)


def test_rglob_fast_silent(monkeypatch):
    _no_env(monkeypatch)
    assert perf_advisory("terminal", {"command": RGLOB_CMD}, 3.0) is None


def test_find_broad_root_fires(monkeypatch):
    _no_env(monkeypatch)
    cmd = "find /home/ubuntu -name '*.ts' | head"
    assert perf_advisory("terminal", {"command": cmd}, 25.0)


def test_find_pruned_silent(monkeypatch):
    _no_env(monkeypatch)
    cmd = "find /home/ubuntu -name node_modules -prune -o -name '*.ts' -print"
    assert perf_advisory("terminal", {"command": cmd}, 25.0) is None


def test_find_maxdepth_silent(monkeypatch):
    _no_env(monkeypatch)
    cmd = "find /home/ubuntu -maxdepth 2 -name '*.ts'"
    assert perf_advisory("terminal", {"command": cmd}, 25.0) is None


def test_find_tmp_silent(monkeypatch):
    _no_env(monkeypatch)
    # /tmp scans are shallow scratch sweeps, not repo walks.
    cmd = "find /tmp -name 'pr-*.log'"
    assert perf_advisory("terminal", {"command": cmd}, 25.0) is None


def test_grep_r_fires(monkeypatch):
    _no_env(monkeypatch)
    cmd = "grep -rn 'request_logs' /home/ubuntu/apifuse"
    assert perf_advisory("terminal", {"command": cmd}, 15.0)


def test_grep_r_exclude_dir_silent(monkeypatch):
    _no_env(monkeypatch)
    cmd = "grep -rn --exclude-dir=node_modules 'request_logs' /home/ubuntu/apifuse"
    assert perf_advisory("terminal", {"command": cmd}, 15.0) is None


# ── sleep-poll advisories ───────────────────────────────────────────


def test_sleep_poll_fires(monkeypatch):
    _no_env(monkeypatch)
    cmd = "while true; do curl -s localhost:8080/health && break; sleep 10; done"
    hint = perf_advisory("terminal", {"command": cmd}, 60.0)
    assert hint and "notify_on_complete" in hint


def test_short_sleep_silent(monkeypatch):
    _no_env(monkeypatch)
    # sleep < 5s inside a fast command is not a polling loop.
    assert perf_advisory("terminal", {"command": "sleep 1; echo ok"}, 11.0) is None


# ── long-foreground advisory ────────────────────────────────────────


def test_long_foreground_fires(monkeypatch):
    _no_env(monkeypatch)
    hint = perf_advisory("terminal", {"command": "pnpm turbo run test"}, 400.0)
    assert hint and "background=true" in hint


def test_long_background_silent(monkeypatch):
    _no_env(monkeypatch)
    args = {"command": "pnpm turbo run test", "background": True}
    assert perf_advisory("terminal", args, 400.0) is None


def test_medium_foreground_silent(monkeypatch):
    _no_env(monkeypatch)
    # Below the 120s foreground threshold, a plain slow command is fine.
    assert perf_advisory("terminal", {"command": "pytest -q"}, 60.0) is None


# ── precedence, scope, knobs ────────────────────────────────────────


def test_tree_scan_wins_over_foreground(monkeypatch):
    _no_env(monkeypatch)
    hint = perf_advisory("terminal", {"command": RGLOB_CMD}, 300.0)
    assert "search_files" in hint


def test_non_terminal_silent(monkeypatch):
    _no_env(monkeypatch)
    assert perf_advisory("execute_code", {"command": RGLOB_CMD}, 88.0) is None
    assert perf_advisory("search_files", {"pattern": "x"}, 88.0) is None


def test_kill_switch(monkeypatch):
    _no_env(monkeypatch)
    monkeypatch.setenv("HERMES_PERF_ADVISOR", "0")
    assert perf_advisory("terminal", {"command": RGLOB_CMD}, 88.0) is None


def test_min_s_override(monkeypatch):
    _no_env(monkeypatch)
    monkeypatch.setenv("HERMES_PERF_ADVISOR_MIN_S", "100")
    assert perf_advisory("terminal", {"command": RGLOB_CMD}, 88.0) is None
    assert perf_advisory("terminal", {"command": RGLOB_CMD}, 120.0)


def test_malformed_args_silent(monkeypatch):
    _no_env(monkeypatch)
    assert perf_advisory("terminal", None, 88.0) is None
    assert perf_advisory("terminal", {"command": None}, 88.0) is None
    assert perf_advisory("terminal", {}, 88.0) is None
