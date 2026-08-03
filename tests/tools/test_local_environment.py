"""Regression tests for credential isolation in local executions."""

import os
from collections.abc import Callable
from unittest.mock import patch

import pytest

from tools.environments.local import _make_run_env, _sanitize_subprocess_env


_GITHUB_TOKEN_ENV_VARS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GITHUB_PAT_TOKEN",
)
_REQUIRED_ENV = {
    "PATH": "/repository/bin:/usr/bin:/bin",
    "GIT_TERMINAL_PROMPT": "0",
    "REPOSITORY_EXECUTION_ID": "wx_test_execution",
}


def _foreground_env(inherited: dict[str, str]) -> dict[str, str]:
    with patch.dict(os.environ, inherited, clear=True):
        return _make_run_env({})


def _background_env(inherited: dict[str, str]) -> dict[str, str]:
    return _sanitize_subprocess_env(inherited)


@pytest.mark.parametrize(
    "build_env",
    [_foreground_env, _background_env],
    ids=["foreground", "background"],
)
def test_local_execution_does_not_inherit_github_tokens(
    build_env: Callable[[dict[str, str]], dict[str, str]],
) -> None:
    inherited = dict(_REQUIRED_ENV)
    inherited.update({name: f"secret-{name}" for name in _GITHUB_TOKEN_ENV_VARS})
    child_env = build_env(inherited)

    assert set(_GITHUB_TOKEN_ENV_VARS).isdisjoint(child_env)
    assert set(_REQUIRED_ENV["PATH"].split(os.pathsep)).issubset(
        child_env["PATH"].split(os.pathsep)
    )
    assert child_env["GIT_TERMINAL_PROMPT"] == "0"
    assert child_env["REPOSITORY_EXECUTION_ID"] == "wx_test_execution"
