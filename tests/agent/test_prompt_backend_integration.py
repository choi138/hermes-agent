"""Remote project context must come from the owning terminal namespace."""

import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent.context_file_sources import list_context_file_sources
from agent.prompt_builder import build_context_files_prompt
from agent.runtime_cwd import resolve_context_cwd
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import terminal_tool as tt
from tools import terminal_tool_backends
from tools.terminal_scope import install_and_reset_profile_terminal_scope


class ShellSSHEnvironment:
    """Run backend commands in an isolated tree; only the SSH wire is replaced."""

    def __init__(self, root: Path, host: str):
        self.root = root
        self.host = host
        self.user = "tester"
        self.port = 22
        self.cwd = "/workspace"
        self.cleaned = False

    def execute(self, command, cwd=None, timeout=8):
        remote_root = self.root / "workspace"
        remote_cwd = str(cwd or self.cwd).replace("/workspace", str(remote_root), 1)
        mapped_command = command.replace("/workspace", str(remote_root))
        result = subprocess.run(
            ["bash", "-c", mapped_command], cwd=remote_cwd,
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "HOME": str(self.root / "home")},
        )
        return {
            "returncode": result.returncode,
            "output": result.stdout.replace(str(remote_root), "/workspace"),
        }

    def cleanup(self):
        self.cleaned = True


@pytest.fixture
def ssh_context_lab(tmp_path, monkeypatch):
    controller = tmp_path / "controller"
    controller.mkdir()
    (controller / "AGENTS.md").write_text("CONTROLLER_ONLY_RULE")
    monkeypatch.chdir(controller)
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_CWD", "/workspace")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "a.invalid")
    monkeypatch.setenv("TERMINAL_SSH_USER", "tester")
    monkeypatch.setattr(tt, "_terminal_config_bridge_attempted", True)
    monkeypatch.setattr(tt, "_active_environments", {})

    def activate(task_id, host):
        root = tmp_path / host
        (root / "workspace" / ".git").mkdir(parents=True)
        (root / "home").mkdir()
        env = ShellSSHEnvironment(root, host)
        tt._active_environments[task_id] = env
        return env

    return activate


def test_remote_context_and_manifest_follow_owning_session_a_b_a(ssh_context_lab):
    alpha = ssh_context_lab("session-a", "a.invalid")
    beta = ssh_context_lab("session-b", "b.invalid")
    (alpha.root / "workspace" / "AGENTS.md").write_text("ALPHA_RULE")
    (beta.root / "workspace" / "AGENTS.md").write_text("BETA_RULE")

    for task_id, expected, excluded in (
        ("session-a", "ALPHA_RULE", "BETA_RULE"),
        ("session-b", "BETA_RULE", "ALPHA_RULE"),
        ("session-a", "ALPHA_RULE", "BETA_RULE"),
    ):
        prompt = build_context_files_prompt(cwd="/workspace", task_id=task_id, skip_soul=True)
        sources = list_context_file_sources(cwd="/workspace", task_id=task_id, skip_soul=True)
        assert expected in prompt
        assert excluded not in prompt and "CONTROLLER_ONLY_RULE" not in prompt
        assert [(source["label"], source["status"]) for source in sources] == [("AGENTS.md", "loaded")]


def test_remote_cwd_is_not_validated_on_controller(ssh_context_lab):
    alpha = ssh_context_lab("default", "a.invalid")
    (alpha.root / "workspace" / "AGENTS.md").write_text("REMOTE_ONLY_RULE")
    assert str(resolve_context_cwd()) == "/workspace"
    assert "REMOTE_ONLY_RULE" in build_context_files_prompt(skip_soul=True)


def test_remote_read_failure_does_not_fall_back_to_controller(ssh_context_lab, monkeypatch):
    alpha = ssh_context_lab("session-a", "a.invalid")
    monkeypatch.setattr(alpha, "execute", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))
    assert build_context_files_prompt(cwd="/workspace", task_id="session-a", skip_soul=True) == ""
    assert list_context_file_sources(cwd="/workspace", task_id="session-a", skip_soul=True) == []


def test_remote_git_boundary_precedence_and_manifest_match(ssh_context_lab):
    env = ssh_context_lab("nested", "a.invalid")
    project = env.root / "workspace"
    (project / "AGENTS.md").write_text("OUTER_RULE")
    nested = project / "nested"
    (nested / "sub").mkdir(parents=True)
    (nested / ".git").write_text("gitdir: /separate")
    (nested / "AGENTS.md").write_text("NESTED_RULE")
    (nested / "sub" / "AGENTS.md").write_text("SHADOWED_RULE")
    (nested / "sub" / "AGENTS.override.md").write_text("OVERRIDE_RULE")
    (nested / "sub" / "CLAUDE.md").write_text("LOWER_PRIORITY_RULE")

    cwd = "/workspace/nested/sub"
    prompt = build_context_files_prompt(cwd=cwd, task_id="nested", skip_soul=True)
    sources = list_context_file_sources(cwd=cwd, task_id="nested", skip_soul=True)
    assert "OUTER_RULE" not in prompt and "SHADOWED_RULE" not in prompt
    assert prompt.index("NESTED_RULE") < prompt.index("OVERRIDE_RULE")
    assert "LOWER_PRIORITY_RULE" not in prompt
    assert {source["label"]: source["status"] for source in sources} == {
        "../AGENTS.md": "loaded", "AGENTS.override.md": "loaded", "CLAUDE.md": "shadowed",
    }


@contextmanager
def _profile_scope(home):
    token = set_hermes_home_override(home)
    try:
        with install_and_reset_profile_terminal_scope(home):
            yield
    finally:
        reset_hermes_home_override(token)


def test_cold_ssh_reads_follow_profile_a_b_a_and_close_only_owned_probes(ssh_context_lab, monkeypatch, tmp_path):
    remote_a = ssh_context_lab("seed-a", "a.invalid")
    remote_b = ssh_context_lab("seed-b", "b.invalid")
    (remote_a.root / "workspace" / "AGENTS.md").write_text("PROFILE_A_RULE")
    (remote_b.root / "workspace" / "AGENTS.md").write_text("PROFILE_B_RULE")
    probes = []

    def create_environment(**kwargs):
        assert kwargs["env_type"] == "ssh" and kwargs["probe_only"] is True
        host = kwargs["ssh_config"]["host"]
        root = remote_a.root if host == "a.invalid" else remote_b.root
        probe = ShellSSHEnvironment(root, host)
        probes.append(probe)
        return probe

    monkeypatch.setattr(terminal_tool_backends, "_create_environment", create_environment)
    homes = {}
    for name, host in (("a", "a.invalid"), ("b", "b.invalid")):
        home = tmp_path / f"profile-{name}"
        home.mkdir()
        (home / "config.yaml").write_text(
            f"terminal:\n  backend: ssh\n  ssh_host: {host}\n  ssh_user: tester\n  cwd: /workspace\n"
        )
        homes[name] = home

    for name, expected, excluded in (
        ("a", "PROFILE_A_RULE", "PROFILE_B_RULE"),
        ("b", "PROFILE_B_RULE", "PROFILE_A_RULE"),
        ("a", "PROFILE_A_RULE", "PROFILE_B_RULE"),
    ):
        with _profile_scope(homes[name]):
            prompt = build_context_files_prompt(skip_soul=True)
            assert expected in prompt and excluded not in prompt
            assert "CONTROLLER_ONLY_RULE" not in prompt
    assert len(probes) == 3 and all(probe.cleaned for probe in probes)
