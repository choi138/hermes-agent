"""Desired behavior checks for remote context; no real network or source edits."""

from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    hermes_home = fake_home / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("terminal:\n  env_type: ssh\n")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_CWD", "/workspace")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "alpha.invalid")
    monkeypatch.setenv("TERMINAL_SSH_USER", "fixture-user")

    from agent import prompt_builder as pb
    from agent import runtime_cwd as rc
    from tools import terminal_tool as tt

    pb._clear_backend_probe_cache()
    rc.clear_session_cwd()
    state = SimpleNamespace(pb=pb, rc=rc, tt=tt, created=[])

    class Environment:
        def __init__(self, host):
            self.host = host

        def execute(self, command, cwd=None, timeout=None):
            return {
                "returncode": 0,
                "output": (
                    f"os=FixtureOS\nkernel=1\nuser={self.host}\n"
                    "home=/fixture-home\ncwd=/workspace\n"
                ),
            }

        def cleanup(self):
            pass

    def create_environment(**kwargs):
        host = kwargs["ssh_config"]["host"]
        state.created.append(host)
        return Environment(host)

    monkeypatch.setattr(tt, "_create_environment", create_environment)
    yield state
    pb._clear_backend_probe_cache()
    rc.clear_session_cwd()


def test_ssh_target_change_refreshes_environment_hint(isolated, monkeypatch):
    first = isolated.pb.build_environment_hints()
    assert "alpha.invalid" in first
    monkeypatch.setenv("TERMINAL_SSH_HOST", "beta.invalid")
    assert isolated.tt._get_env_config()["ssh_host"] == "beta.invalid"
    second = isolated.pb.build_environment_hints()
    assert "beta.invalid" in second, (
        "The active terminal target is beta, but prompt still describes alpha; "
        f"probed targets={isolated.created}; hint={second!r}"
    )
    assert "alpha.invalid" not in second


def test_same_ssh_target_reuses_probe(isolated):
    first = isolated.pb.build_environment_hints()
    second = isolated.pb.build_environment_hints()
    assert first == second
    assert isolated.created == ["alpha.invalid"]


def test_missing_remote_path_does_not_load_controller_instructions(
    isolated, tmp_path, monkeypatch
):
    controller = tmp_path / "controller"
    controller.mkdir()
    (controller / "AGENTS.md").write_text("CONTROLLER_ONLY_FIXTURE_INSTRUCTION")
    monkeypatch.chdir(controller)
    # The remote namespace is intentionally absent from the controller FS.
    remote_path = "/hermes-remote-fixture-only/project"
    assert not Path(remote_path).exists()
    monkeypatch.setenv("TERMINAL_CWD", remote_path)
    isolated.rc.set_session_cwd(remote_path)
    context = isolated.pb.build_context_files_prompt(
        cwd=isolated.rc.resolve_context_cwd(),
        skip_soul=True,
        allow_install_tree_fallback=False,
    )
    assert "CONTROLLER_ONLY_FIXTURE_INSTRUCTION" not in context, (
        "An explicit remote workspace silently fell back to controller AGENTS.md"
    )


def test_existing_local_project_loads_its_own_instructions(
    isolated, tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("LOCAL_PROJECT_FIXTURE_INSTRUCTION")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    isolated.rc.set_session_cwd(str(project))
    context = isolated.pb.build_context_files_prompt(
        cwd=isolated.rc.resolve_context_cwd(), skip_soul=True
    )
    assert "LOCAL_PROJECT_FIXTURE_INSTRUCTION" in context
