"""Cold schema discovery must not promote presence into execution readiness."""

import os
from unittest.mock import Mock

import pytest

from tools import browser_tool as browser
from tools import browser_tool_install as install


@pytest.fixture
def isolated_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "tools"))
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    monkeypatch.delenv("AGENT_BROWSER_EXECUTABLE_PATH", raising=False)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setattr(browser, "_SANE_PATH_DIRS", [])
    monkeypatch.setattr(install, "_is_termux_environment", lambda: False)
    monkeypatch.setattr(install, "_discover_homebrew_node_dirs", lambda: ())
    # PM lazy acquisition is disabled; fixture subprocesses inherit no credentials.
    return tmp_path


def executable(home, name="npx", code=0):
    directory = home / "node" / "bin"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    marker = home / (name + ".probes")
    if os.name == "nt":
        path = path.with_suffix(".cmd")
        path.write_text(f'@echo off\necho %1>>"{marker}"\nexit /b {code}\n')
    else:
        path.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$1\" >> '{marker}'\nexit {code}\n")
        path.chmod(0o755)
    return path, marker


@pytest.mark.parametrize("code", [0, 127])
def test_npx_is_never_a_browser_fallback(isolated_browser, monkeypatch, code):
    path, marker = executable(isolated_browser, code=code)
    monkeypatch.setenv("PATH", str(path.parent))
    for validate in (False, True):
        with pytest.raises(FileNotFoundError, match="agent-browser CLI not found"):
            install._find_agent_browser(validate=validate)
    assert not marker.exists()


def publish_pm_browser(home):
    """Publish a real target/lock-bound PM fact without downloading or executing."""
    import pm
    from pm import paths

    store = home / "tools"
    package = pm.get_package("agent-browser")
    lock = pm.Lockfile(paths.lockfile_path())
    target = pm.current_target()
    version = lock.version(package.name)
    assert version is not None
    entry = store / package.store_entry(version, target)
    binary = package.binary(entry, target)
    assert binary is not None
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("fixture binary; discovery must never execute this")
    binary.chmod(0o755)
    pm.Facts(store / "facts.json").record(
        package.name, version, entry.name, package.env(entry, target), store,
        target=target, artifacts=[a["sha256"] for a in lock.artifacts(package.name, target)],
    )
    return binary


def test_schema_pm_discovery_never_executes_or_acquires(isolated_browser, monkeypatch):
    import pm

    binary = publish_pm_browser(isolated_browser)
    probe = Mock(side_effect=AssertionError("cold discovery executed a browser"))
    acquire = Mock(side_effect=AssertionError("cold discovery acquired a browser"))
    monkeypatch.setattr(install, "agent_browser_runnable", probe)
    monkeypatch.setattr(pm, "ensure", acquire)
    assert install._find_agent_browser(validate=False) == str(binary)
    probe.assert_not_called()
    acquire.assert_not_called()


@pytest.mark.parametrize("kind", ["missing", "dangling"])
def test_bad_external_browser_paths_are_not_present(isolated_browser, monkeypatch, kind):
    path, marker = executable(isolated_browser, "agent-browser")
    monkeypatch.setenv("PATH", str(path.parent))
    path.unlink()
    if kind == "dangling":
        path.symlink_to(path.parent / "absent")
    with pytest.raises(FileNotFoundError):
        install._find_agent_browser(validate=False)
    assert not marker.exists()


def assert_nonexecutable_browser_absent(home, monkeypatch):
    path, marker = executable(home, "agent-browser")
    monkeypatch.setenv("PATH", str(path.parent))
    path.chmod(0o644)
    with pytest.raises(FileNotFoundError):
        install._find_agent_browser(validate=False)
    assert not marker.exists()


@pytest.mark.platforms("linux")
def test_linux_nonexecutable_browser_is_not_present(isolated_browser, monkeypatch):
    assert_nonexecutable_browser_absent(isolated_browser, monkeypatch)


@pytest.mark.platforms("macos")
def test_macos_nonexecutable_browser_is_not_present(isolated_browser, monkeypatch):
    assert_nonexecutable_browser_absent(isolated_browser, monkeypatch)


@pytest.mark.parametrize("code", [0, 127])
def test_agent_browser_presence_does_not_cache_execution(isolated_browser, monkeypatch, code):
    path, marker = executable(isolated_browser, "agent-browser", code)
    monkeypatch.setenv("PATH", str(path.parent))
    assert install._find_agent_browser(validate=False) == str(path)
    assert not marker.exists()
    if code:
        with pytest.raises(FileNotFoundError):
            install._find_agent_browser(validate=True)
    else:
        assert install._find_agent_browser(validate=True) == str(path)
    assert marker.read_text().splitlines() == ["--version"]


def test_repeated_cold_readiness_tracks_profile_switches(isolated_browser, monkeypatch):
    from agent import secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    a, b = isolated_browser / "a", isolated_browser / "b"
    publish_pm_browser(a)
    b.mkdir()
    monkeypatch.setattr(browser, "_is_browser_use_cli_mode", lambda: False)
    monkeypatch.setattr(browser, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(install._cdp, "_get_cdp_override_raw", lambda: "")
    monkeypatch.setattr(install._cloud, "_get_cloud_provider", lambda: None)
    monkeypatch.setattr(install._lp, "_using_lightpanda_engine", lambda: False)
    monkeypatch.setattr(install, "_chromium_installed", lambda: True)
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", Mock(side_effect=AssertionError("cold readiness spawned a process")))
    for home, expected in [(a, True), (b, False), (a, True)]:
        # Each configured profile points at its own native PM runtime store.
        monkeypatch.setenv("HERMES_RUNTIME_DIR", str(home / "tools"))
        token = set_hermes_home_override(home)
        scope = secret_scope.set_secret_scope({})
        try:
            for _ in range(13):
                assert install.check_browser_requirements() is expected
        finally:
            secret_scope.reset_secret_scope(scope)
            reset_hermes_home_override(token)


def test_unconfigured_cdp_never_checks_browser(isolated_browser, monkeypatch):
    from tools import browser_cdp_tool as cdp
    from tools import browser_tool_cdp as endpoint
    monkeypatch.setattr(endpoint, "_get_cdp_override_raw", lambda: "")
    probe = Mock(side_effect=AssertionError("unconfigured CDP checked browser"))
    monkeypatch.setattr(install, "check_browser_requirements", probe)
    assert cdp._browser_cdp_check() is False
    probe.assert_not_called()


def test_cold_production_schema_assembly_is_spawn_free_across_profiles(isolated_browser, monkeypatch):
    # Fail explicitly rather than silently exercising model_tools' optional-import fallback.
    from tools import tool_search
    import model_tools as model
    import tools.registry as registry_module
    from agent import secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools import vision_tools

    a, b = isolated_browser / "a", isolated_browser / "b"
    publish_pm_browser(a)
    b.mkdir()
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    monkeypatch.setattr(browser, "_is_browser_use_cli_mode", lambda: False)
    monkeypatch.setattr(browser, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(install._cdp, "_get_cdp_override_raw", lambda: "")
    monkeypatch.setattr(install._cloud, "_get_cloud_provider", lambda: None)
    monkeypatch.setattr(install._lp, "_using_lightpanda_engine", lambda: False)
    monkeypatch.setattr(install, "_chromium_installed", lambda: True)
    monkeypatch.setattr(vision_tools, "check_vision_requirements", lambda: False)
    monkeypatch.setattr(model, "_tool_defs_cache", {})
    import pm
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", Mock(side_effect=AssertionError("cold schema spawned a process")))
    monkeypatch.setattr(install, "agent_browser_runnable", Mock(side_effect=AssertionError("cold schema executed browser")))
    monkeypatch.setattr(pm, "ensure", Mock(side_effect=AssertionError("cold schema acquired browser")))
    for home, present in [(a, True), (b, False), (a, True)]:
        # Each configured profile points at its own native PM runtime store.
        monkeypatch.setenv("HERMES_RUNTIME_DIR", str(home / "tools"))
        token = set_hermes_home_override(home)
        scope = secret_scope.set_secret_scope({})
        try:
            for _ in range(3):
                model._tool_defs_cache.clear()
                registry_module.invalidate_check_fn_cache()
                defs = model.get_tool_definitions(["browser"], quiet_mode=True)
                raw = model.get_tool_definitions(["browser"], quiet_mode=True, skip_tool_search_assembly=True)
                assert defs == tool_search.assemble_tool_defs(raw).tool_defs
                assert ("browser_navigate" in {td["function"]["name"] for td in defs}) is present
        finally:
            secret_scope.reset_secret_scope(scope)
            reset_hermes_home_override(token)


def test_configured_cdp_retains_browser_gate(isolated_browser, monkeypatch):
    from tools import browser_cdp_tool as cdp
    from tools import browser_tool_cdp as endpoint
    monkeypatch.setattr(endpoint, "_get_cdp_override_raw", lambda: "ws://127.0.0.1:9222")
    probe = Mock(return_value=False)
    monkeypatch.setattr(install, "check_browser_requirements", probe)
    assert cdp._browser_cdp_check() is False
    probe.assert_called_once_with()
