"""Cold schema discovery must not promote presence into execution readiness.

Upstream removed agent-browser (68500d9ff9); the CDP gate is what remains of the cold-readiness
contract: an unconfigured CDP endpoint must not probe the browser surface at all.
"""

from unittest.mock import Mock


def test_unconfigured_cdp_never_checks_browser(monkeypatch):
    from tools import browser_cdp_tool as cdp
    from tools import browser_session_cdp as endpoint
    from tools import browser_use_cli
    monkeypatch.setattr(endpoint, "get_cdp_override_raw", lambda: "")
    probe = Mock(side_effect=AssertionError("unconfigured CDP checked browser"))
    monkeypatch.setattr(browser_use_cli, "browser_exec_available", probe)
    assert cdp._browser_cdp_check() is False
    probe.assert_not_called()


def test_configured_cdp_retains_browser_gate(monkeypatch):
    from tools import browser_cdp_tool as cdp
    from tools import browser_session_cdp as endpoint
    from tools import browser_use_cli
    monkeypatch.setattr(endpoint, "get_cdp_override_raw", lambda: "ws://127.0.0.1:9222")
    probe = Mock(return_value=False)
    monkeypatch.setattr(browser_use_cli, "browser_exec_available", probe)
    assert cdp._browser_cdp_check() is False
    probe.assert_called_once_with()
