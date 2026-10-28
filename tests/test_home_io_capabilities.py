"""The home guard preserves usable descriptor-relative filesystem capabilities."""
import os

import pytest


@pytest.mark.platforms("posix")
def test_guarded_open_preserves_safe_dir_fd_capability(tmp_path, monkeypatch):
    from tests.home_io_guard import HomeIOGuard

    safe = tmp_path / "safe"
    safe.mkdir()
    (safe / "value").write_bytes(b"ok")
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "value").write_bytes(b"secret")
    with monkeypatch.context() as patcher:
        HomeIOGuard(lambda: [protected]).install(patcher)
        assert os.open in os.supports_dir_fd
        original = os.open(safe, os.O_RDONLY | os.O_DIRECTORY)
        directory = os.dup(original)
        os.close(original)
        try:
            fd = os.open("value", os.O_RDONLY, dir_fd=directory)
            try:
                assert os.read(fd, 2) == b"ok"
            finally:
                os.close(fd)
            with pytest.raises(AssertionError, match="REAL hermes home"):
                os.open("../protected/value", os.O_RDONLY, dir_fd=directory)
        finally:
            os.close(directory)
