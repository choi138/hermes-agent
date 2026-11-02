"""Real Git inventories remain bounded while supporting scoped monorepo work."""
import subprocess

import pytest

from agent.delegation_progress import Manifest, collect
from tests.agent.test_delegation_progress import git, lane as lane_fixture

lane = lane_fixture


@pytest.mark.parametrize("entries, available", [(25_000, True), (80_000, False)])
def test_scoped_inventory_with_large_git_metadata(lane, entries, available):
    repo, _, data, _ = lane
    oid = git(repo, "rev-parse", "HEAD:runner.py").decode().strip()
    # Populate a real index/tree without creating tens of thousands of files.
    records = b"".join(
        f"100644 {oid}\tassets/{number:06d}-{'x' * 180}.bin\0".encode()
        for number in range(entries)
    )
    subprocess.run(["git", "-C", str(repo), "update-index", "-z", "--index-info"],
                   input=records, check=True, capture_output=True)
    git(repo, "commit", "-qm", "large metadata fixture")
    # The successful case crosses the old ceiling for all three inventories.
    for args in (("ls-files", "--stage", "-z"), ("ls-tree", "-rz", "HEAD"),
                 ("status", "--porcelain=v1", "-z", "--untracked-files=all")):
        assert len(git(repo, *args)) > 4 * 1024 * 1024
    data["code_scope"] = ["runner.py"]
    result = collect(Manifest.from_dict(data))
    assert result["available"] is available
    if available:
        assert result["errors"] == []
        assert set(result["files"]) == {"runner.py"}
        assert result["files"]["runner.py"]["known"]
        assert result["files"]["runner.py"]["content"] is not None
    else:
        assert "git_truncated" in result["errors"]
        assert "git_unavailable" in result["errors"]
