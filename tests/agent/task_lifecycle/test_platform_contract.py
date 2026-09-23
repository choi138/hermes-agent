"""Portable rejection contract; never fake successful Darwin support on Linux."""
import sys
from types import SimpleNamespace

import pytest

from agent.task_lifecycle import directory_handoff as handoff
from agent.task_lifecycle.contract import ExecutionAuthority
from agent.task_lifecycle.types import LifecycleError


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_unsupported_platform_rejects_before_directory_acquisition(monkeypatch, platform, tmp_path):
    # Patch only the module reference, not global sys.platform used by pytest.
    monkeypatch.setattr(handoff, "sys", SimpleNamespace(platform=platform))
    with pytest.raises(LifecycleError, match="requires Darwin openat/fchdir/kqueue"):
        handoff.require_support()
    with pytest.raises(LifecycleError, match="requires Darwin openat/fchdir/kqueue"):
        with handoff.open_directory(tmp_path):
            pytest.fail("unsupported platform acquired a directory")
    with pytest.raises(LifecycleError, match="requires Darwin openat/fchdir/kqueue"):
        ExecutionAuthority("alice", "discord:123", "1", "default",
                           str(tmp_path), str(tmp_path), (str(tmp_path),))


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_unsupported_platform_never_spawns_workload(monkeypatch, platform):
    monkeypatch.setattr(handoff, "sys", SimpleNamespace(platform=platform))
    with pytest.raises(LifecycleError, match="requires Darwin openat/fchdir/kqueue"):
        handoff.spawn_pinned(lambda *a, **kw: pytest.fail("must not spawn"), [],
                             directory_fd=-1, request=None, deadline=0)


def test_native_platform_support_contract():
    if sys.platform == "darwin":
        handoff.require_support()
    else:
        with pytest.raises(LifecycleError, match="requires Darwin openat/fchdir/kqueue"):
            handoff.require_support()


def test_missing_darwin_kernel_capability_is_rejected(monkeypatch):
    monkeypatch.setattr(handoff, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(handoff, "select", SimpleNamespace())
    with pytest.raises(LifecycleError, match="requires Darwin openat/fchdir/kqueue"):
        handoff.require_support()
