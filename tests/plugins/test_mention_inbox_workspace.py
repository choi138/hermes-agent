from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
from multiprocessing.connection import Connection
from pathlib import Path
from unittest.mock import patch

import pytest

from plugins.mention_inbox.workspace import (
    RepositoryWorktreeManager,
    WorktreeRequest,
    WorkspaceConflictError,
    _default_git_runner,
    _repository_process_lock,
)


class FakeGitRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: tuple[str, ...]) -> str:
        self.calls.append(argv)
        if argv[:2] == ("git", "clone"):
            Path(argv[-1]).mkdir(parents=True)
            return ""
        if "worktree" in argv and "add" in argv:
            Path(argv[-2]).mkdir(parents=True)
            return ""
        if argv[-2:] == ("status", "--porcelain"):
            return ""
        if argv[-2:] == ("rev-parse", "HEAD"):
            return "a" * 40
        if argv[-3:] == ("remote", "get-url", "origin"):
            return "https://github.com/owner/repo.git"
        return ""


def _request() -> WorktreeRequest:
    return WorktreeRequest(
        execution_id="wx_1234567890abcdef12345678",
        repository_node_id="R_repo",
        base_repository="owner/repo",
        head_repository="contributor/repo",
        head_ref="feature/fix",
        head_sha="a" * 40,
    )


def _hold_repository_lock(
    root: str,
    ready: Connection,
    release: Connection,
) -> None:
    with _repository_process_lock(Path(root), "R_repo"):
        ready.send(True)
        release.recv()


def _probe_repository_lock(root: str, result: Connection) -> None:
    try:
        with _repository_process_lock(Path(root), "R_repo", blocking=False):
            result.send(True)
    except BlockingIOError:
        result.send(False)


def test_repository_cache_creates_execution_owned_worktree(tmp_path: Path) -> None:
    runner = FakeGitRunner()
    manager = RepositoryWorktreeManager(tmp_path, runner=runner)

    workspace = manager.prepare(_request())

    assert workspace == (
        tmp_path / "executions" / "wx_1234567890abcdef12345678"
    ).resolve()
    marker_path = (
        tmp_path / "ownership" / "wx_1234567890abcdef12345678.json"
    )
    marker = json.loads(marker_path.read_text())
    assert not (workspace / ".hermes-execution.json").exists()
    assert marker == {
        "base_repository": "owner/repo",
        "execution_id": "wx_1234567890abcdef12345678",
        "head_ref": "feature/fix",
        "head_repository": "contributor/repo",
        "head_sha": "a" * 40,
        "repository_node_id": "R_repo",
    }
    assert all(isinstance(call, tuple) for call in runner.calls)
    assert not any(
        forbidden in call
        for call in runner.calls
        for forbidden in ("reset", "stash", "clean")
    )


def test_matching_clean_worktree_is_reused_without_recreation(tmp_path: Path) -> None:
    runner = FakeGitRunner()
    manager = RepositoryWorktreeManager(tmp_path, runner=runner)
    request = _request()
    workspace = manager.prepare(request)
    first_calls = tuple(runner.calls)

    assert manager.prepare(request) == workspace
    assert tuple(runner.calls[: len(first_calls)]) == first_calls
    assert sum("worktree" in call and "add" in call for call in runner.calls) == 1


def test_repository_lock_excludes_another_process(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready_reader, ready_writer = context.Pipe(duplex=False)
    release_reader, release_writer = context.Pipe(duplex=False)
    result_reader, result_writer = context.Pipe(duplex=False)
    holder = context.Process(
        target=_hold_repository_lock,
        args=(str(tmp_path), ready_writer, release_reader),
    )
    probe = context.Process(
        target=_probe_repository_lock,
        args=(str(tmp_path), result_writer),
    )

    holder.start()
    assert ready_reader.poll(5)
    assert ready_reader.recv() is True
    probe.start()
    assert result_reader.poll(5)
    assert result_reader.recv() is False
    release_writer.send(True)
    holder.join(5)
    probe.join(5)

    assert holder.exitcode == 0
    assert probe.exitcode == 0


def test_default_git_runner_uses_anonymous_environment(tmp_path: Path) -> None:
    anonymous_home = tmp_path / "anonymous-home"
    inherited = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "credentialed-home"),
        "XDG_CONFIG_HOME": str(tmp_path / "credentialed-config"),
        "GH_TOKEN": "secret",
        "GITHUB_PAT_TOKEN": "secret",
        "GIT_ASKPASS": "/tmp/credential-helper",
        "GIT_SSH_COMMAND": "ssh -i /tmp/private-key",
    }
    completed = subprocess.CompletedProcess(
        ("git", "--version"),
        0,
        stdout="git version test",
    )

    with (
        patch.dict(os.environ, inherited, clear=True),
        patch(
            "plugins.mention_inbox.workspace.subprocess.run",
            return_value=completed,
        ) as run,
    ):
        output = _default_git_runner(
            ("git", "--version"),
            home=anonymous_home,
        )

    child_env = run.call_args.kwargs["env"]
    assert output == "git version test"
    assert child_env["HOME"] == str(anonymous_home)
    assert child_env["XDG_CONFIG_HOME"] == str(anonymous_home)
    assert child_env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert child_env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert child_env["GIT_TERMINAL_PROMPT"] == "0"
    assert {
        "GH_TOKEN",
        "GITHUB_PAT_TOKEN",
        "GIT_ASKPASS",
        "GIT_SSH_COMMAND",
    }.isdisjoint(child_env)


def test_foreign_or_dirty_worktree_fails_closed(tmp_path: Path) -> None:
    runner = FakeGitRunner()
    manager = RepositoryWorktreeManager(tmp_path, runner=runner)
    request = _request()
    workspace = manager.prepare(request)
    marker_path = (
        tmp_path / "ownership" / "wx_1234567890abcdef12345678.json"
    )
    marker = json.loads(marker_path.read_text())
    marker["execution_id"] = "wx_foreign"
    marker_path.write_text(json.dumps(marker))

    with pytest.raises(WorkspaceConflictError, match="different execution"):
        manager.prepare(request)

    assert not any(
        forbidden in call
        for call in runner.calls
        for forbidden in ("reset", "stash", "clean", "remove")
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository_node_id", "../repo"),
        ("base_repository", "owner/repo/extra"),
        ("head_repository", "https://github.com/attacker/repo"),
        ("head_ref", "../main"),
        ("head_sha", "not-a-sha"),
    ],
)
def test_worktree_request_rejects_untrusted_coordinates(
    field: str,
    value: str,
) -> None:
    values = _request().__dict__ | {field: value}

    with pytest.raises(ValueError):
        WorktreeRequest(**values)
