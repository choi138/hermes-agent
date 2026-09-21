"""Exercise shell matcher admission and bounded callback serialization."""

import shlex
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from agent.shell_hooks import ShellHookSpec, _make_callback
from hermes_cli import plugins


def test_unmatched_tool_bypasses_running_and_suppressed_hook(tmp_path, monkeypatch):
    monkeypatch.setattr(plugins, "_resolve_hook_callback_timeout", lambda: 0.2)
    manager = plugins.PluginManager()
    entered, release = threading.Event(), threading.Event()
    callback = _make_callback(ShellHookSpec("pre_tool_call", "policy", matcher="read_file"))

    def spawn(*_args):
        entered.set()
        release.wait(5)
        return {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False}

    monkeypatch.setattr("agent.shell_hooks._spawn", spawn)
    manager._hooks["pre_tool_call"] = [callback]
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(manager.invoke_hook, "pre_tool_call", tool_name="read_file")
        try:
            assert entered.wait(2)
            assert manager.invoke_hook("pre_tool_call", tool_name="discord") == []
            assert first.result(timeout=2)[0]["action"] == "block"
            assert manager.invoke_hook("pre_tool_call", tool_name="discord") == []
            assert manager.invoke_hook("pre_tool_call", tool_name="read_file")[0]["action"] == "block"
        finally:
            release.set()


def test_concurrent_matching_shell_checks_serialize_and_preserve_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(plugins, "_resolve_hook_callback_timeout", lambda: 3)
    manager = plugins.PluginManager()
    script = tmp_path / "policy.py"
    entered = tmp_path / "entered"
    release = tmp_path / "release"
    # A real subprocess lock detects accidental concurrent policy execution.
    script.write_text(
        "import json, os, pathlib, sys, time\n"
        f"root = pathlib.Path({str(tmp_path)!r})\n"
        "payload = json.load(sys.stdin)\n"
        "fd = os.open(root / 'lock', os.O_CREAT | os.O_EXCL | os.O_WRONLY)\n"
        "try:\n"
        "    (root / 'entered').touch()\n"
        "    while not (root / 'release').exists(): time.sleep(0.01)\n"
        "    if payload['tool_input'].get('deny'): print(json.dumps({'decision':'block','reason':'policy denied'}))\n"
        "finally:\n"
        "    os.close(fd)\n"
        "    (root / 'lock').unlink()\n",
        encoding="utf-8",
    )
    callback = _make_callback(ShellHookSpec(
        "pre_tool_call", f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}",
        matcher="read_file", fail_closed=True,
    ))
    manager._hooks["pre_tool_call"] = [callback]
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(manager.invoke_hook, "pre_tool_call", tool_name="read_file", args={})
        try:
            # File synchronization crosses the real shell subprocess boundary.
            for _ in range(200):
                if entered.exists():
                    break
                threading.Event().wait(0.01)
            assert entered.exists()
            second = pool.submit(manager.invoke_hook, "pre_tool_call", tool_name="read_file", args={"deny": True})
            # Ensure the second invocation encounters the occupied callback.
            threading.Event().wait(0.1)
        finally:
            release.touch()
        assert first.result(timeout=4) == []
        assert second.result(timeout=4) == [{"action": "block", "message": "policy denied"}]


def test_waiting_invocation_has_its_own_bounded_budget(monkeypatch):
    monkeypatch.setattr(plugins, "_resolve_hook_callback_timeout", lambda: 0.15)
    manager = plugins.PluginManager()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def policy(**kwargs):
        calls.append(kwargs)
        entered.set()
        release.wait(5)

    manager._hooks["pre_tool_call"] = [policy]
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(manager.invoke_hook, "pre_tool_call", tool_name="read_file")
        try:
            assert entered.wait(2)
            second = pool.submit(manager.invoke_hook, "pre_tool_call", tool_name="read_file")
            assert first.result(timeout=2)[0]["action"] == "block"
            assert second.result(timeout=2)[0]["action"] == "block"
            assert len(calls) == 1, "hung callbacks must not accumulate workers"
        finally:
            release.set()
