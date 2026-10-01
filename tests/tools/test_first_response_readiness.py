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
    monkeypatch.setattr(browser, "_cached_agent_browser", None)
    monkeypatch.setattr(browser, "_agent_browser_resolved", False)
    monkeypatch.setattr(browser, "_SANE_PATH_DIRS", [])
    monkeypatch.setattr(install, "_is_termux_environment", lambda: False)
    monkeypatch.setattr(install, "_discover_homebrew_node_dirs", lambda: ())
    monkeypatch.setattr(install, "_agent_browser_candidates", lambda path: iter(()))
    # Never install dependencies or inherit credentials into fixture processes.
    import hermes_cli.dep_ensure as dep
    import hermes_constants as constants
    monkeypatch.setattr(dep, "ensure_dependency", lambda name: False)
    monkeypatch.setattr(constants, "with_hermes_node_path", lambda: {"PATH": ""})
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


def test_schema_npx_fallback_never_validates(isolated_browser, monkeypatch):
    path, marker = executable(isolated_browser)
    probe = Mock(side_effect=AssertionError("schema discovery spawned npx validation"))
    monkeypatch.setattr(install, "node_tool_runnable", probe)
    assert install._find_agent_browser(validate=False) == browser.NPX_AGENT_BROWSER_SENTINEL
    probe.assert_not_called()
    assert not marker.exists()
    assert not browser._agent_browser_resolved
    assert browser._cached_agent_browser is None


@pytest.mark.parametrize("code", [0, 127])
def test_npx_execution_really_probes_after_discovery(isolated_browser, code):
    path, marker = executable(isolated_browser, code=code)
    assert install._find_agent_browser(validate=False) == browser.NPX_AGENT_BROWSER_SENTINEL
    assert not marker.exists()
    if code:
        with pytest.raises(FileNotFoundError):
            install._find_agent_browser(validate=True)
        assert browser._cached_agent_browser is None
    else:
        assert install._find_agent_browser(validate=True) == browser.NPX_AGENT_BROWSER_SENTINEL
    assert marker.read_text().splitlines() == ["--version"]


@pytest.mark.parametrize("kind", ["missing", "dangling"])
def test_bad_npx_paths_are_not_present(isolated_browser, kind):
    path, marker = executable(isolated_browser)
    path.unlink()
    if kind == "dangling":
        path.symlink_to(path.parent / "absent")
    with pytest.raises(FileNotFoundError):
        install._find_agent_browser(validate=False)
    assert not marker.exists()
    assert not browser._agent_browser_resolved


def assert_nonexecutable_npx_absent(home):
    path, marker = executable(home)
    path.chmod(0o644)
    with pytest.raises(FileNotFoundError):
        install._find_agent_browser(validate=False)
    assert not marker.exists()
    assert not browser._agent_browser_resolved


@pytest.mark.linux_only
def test_linux_nonexecutable_npx_is_not_present(isolated_browser):
    assert_nonexecutable_npx_absent(isolated_browser)


@pytest.mark.macos_only
def test_macos_nonexecutable_npx_is_not_present(isolated_browser):
    assert_nonexecutable_npx_absent(isolated_browser)


@pytest.mark.parametrize("code", [0, 127])
def test_agent_browser_presence_does_not_cache_execution(isolated_browser, monkeypatch, code):
    path, marker = executable(isolated_browser, "agent-browser", code)
    monkeypatch.setattr(install, "_agent_browser_candidates", lambda extended: iter([str(path)]))
    assert install._find_agent_browser(validate=False) == str(path)
    assert not marker.exists()
    assert browser._cached_agent_browser is None
    if code:
        with pytest.raises(FileNotFoundError):
            install._find_agent_browser(validate=True)
        assert browser._cached_agent_browser is None
    else:
        assert install._find_agent_browser(validate=True) == str(path)
    assert marker.read_text().splitlines() == ["--version"]


def test_repeated_cold_readiness_tracks_profile_switches(isolated_browser, monkeypatch):
    from agent import secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    a, b = isolated_browser / "a", isolated_browser / "b"
    path, marker = executable(a)
    b.mkdir()
    monkeypatch.setattr(browser, "_is_browser_use_cli_mode", lambda: False)
    monkeypatch.setattr(browser, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(install._cdp, "_get_cdp_override_raw", lambda: "")
    monkeypatch.setattr(install._cloud, "_get_cloud_provider", lambda: None)
    monkeypatch.setattr(install._lp, "_using_lightpanda_engine", lambda: False)
    monkeypatch.setattr(install, "_chromium_installed", lambda: True)
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    for home, expected in [(a, True), (b, False), (a, True)]:
        token = set_hermes_home_override(home)
        scope = secret_scope.set_secret_scope({})
        try:
            for _ in range(13):
                assert install.check_browser_requirements() is expected
                assert not browser._agent_browser_resolved
        finally:
            secret_scope.reset_secret_scope(scope)
            reset_hermes_home_override(token)
    assert not marker.exists()


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
    path, marker = executable(a)
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
    monkeypatch.setattr(install, "node_tool_runnable", Mock(side_effect=AssertionError("cold schema spawned npx")))
    for home, present in [(a, True), (b, False), (a, True)]:
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
                assert not browser._agent_browser_resolved
        finally:
            secret_scope.reset_secret_scope(scope)
            reset_hermes_home_override(token)
    assert not marker.exists()


def test_configured_cdp_retains_browser_gate(isolated_browser, monkeypatch):
    from tools import browser_cdp_tool as cdp
    from tools import browser_tool_cdp as endpoint
    monkeypatch.setattr(endpoint, "_get_cdp_override_raw", lambda: "ws://127.0.0.1:9222")
    probe = Mock(return_value=False)
    monkeypatch.setattr(install, "check_browser_requirements", probe)
    assert cdp._browser_cdp_check() is False
    probe.assert_called_once_with()
