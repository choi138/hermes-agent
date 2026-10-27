"""Operational login environment is fixed; model arguments never become shell code."""
import json
import shlex
import sys

import pytest

from scripts import run_codex_task as launcher


class Reexecuted(Exception):
    pass


@pytest.mark.parametrize('flag', [['--progress-login-shell', 'zsh'], ['--progress-login-shell=zsh']])
def test_fixed_zsh_reexec_preserves_opaque_environment_and_quoted_arguments(monkeypatch, flag):
    captured = {}
    def reexec(binary, argv, env):
        captured.update(binary=binary, argv=argv, env=env)
        raise Reexecuted
    monkeypatch.setattr(launcher.os, 'execvpe', reexec)
    monkeypatch.setenv('ASDF_NODEJS_VERSION', 'old-default')
    monkeypatch.setenv('HERMES_OPAQUE_ENV_FIXTURE', 'preserved')
    original = ['--spec', '/tmp/a $(touch sentinel) SPEC.md', '--workdir', '/tmp/a space',
                '--progress-owner', 'gateway', '--progress-code-scope', 'nested/module.py',
                '--progress-sender-profile', 'koharu', '--cli', 'claude', '--model', 'sonnet', '--advisor', 'opus']
    with pytest.raises(Reexecuted):
        launcher.reexec_login_shell([*original, *flag])
    assert captured['binary'] == '/bin/zsh'
    assert captured['argv'][:2] == ['/bin/zsh', '-ic']
    decoded = shlex.split(captured['argv'][2])
    assert decoded[:2] == ['ASDF_NODEJS_VERSION=22.19.0', 'exec']
    assert decoded[2] == sys.executable
    assert decoded[4:] == original
    assert '--progress-login-shell' not in decoded
    assert captured['env']['ASDF_NODEJS_VERSION'] == '22.19.0'
    assert captured['env']['HERMES_OPAQUE_ENV_FIXTURE'] == 'preserved'
    assert launcher.os.environ['ASDF_NODEJS_VERSION'] == 'old-default'


def test_non_gateway_execution_rejects_login_shell_before_registration(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['run_codex_task.py', '--spec', '/tmp/SPEC.md',
        '--workdir', '/tmp', '--allowed-root', '/tmp', '--output-dir', '/tmp/output',
        '--progress-login-shell', 'zsh'])
    monkeypatch.setattr(launcher.os, 'execvpe', lambda *args: pytest.fail('must not reexec'))
    assert launcher.main() == 74
    assert 'progress_login_shell_requires_gateway_owner' in capsys.readouterr().err


def test_dry_run_ignores_login_reexec_and_preserves_policy(monkeypatch, capsys, tmp_path):
    spec = tmp_path/'SPEC.md'
    spec.write_text('Inspect only.')
    output = tmp_path/'output'
    output.mkdir(mode=0o700)
    monkeypatch.setattr(sys, 'argv', ['run_codex_task.py', '--spec', str(spec),
        '--workdir', str(tmp_path), '--allowed-root', str(tmp_path), '--output-dir', str(output),
        '--progress-login-shell', 'zsh', '--dry-run', '--task-class', 'mechanical'])
    monkeypatch.setattr(launcher.os, 'execvpe', lambda *args: pytest.fail('must not reexec dry run'))
    assert launcher.main() == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['policy']['selected'] == {'model': 'gpt-6.1-terra', 'effort': 'low'}
    assert list(output.iterdir()) == []


def test_fixed_command_executes_real_zsh_without_argument_expansion(tmp_path, monkeypatch):
    import os
    import subprocess
    from pathlib import Path
    if not Path('/bin/zsh').exists():
        pytest.skip('zsh is required for real shell gate')
    receiver = tmp_path/'argv receiver.py'
    output = tmp_path/'received.json'
    receiver.write_text('import sys,json,os\nfrom pathlib import Path\n'
        'Path(sys.argv[1]).write_text(json.dumps({"argv":sys.argv[2:],"node":os.environ["ASDF_NODEJS_VERSION"]}))\n')
    zdot = tmp_path/'zsh-config'
    zdot.mkdir()
    (zdot/'.zshrc').write_text('export ASDF_NODEJS_VERSION=wrong-default\n')
    captured = {}
    def reexec(binary, argv, environment):
        captured.update(binary=binary, argv=argv, environment=environment)
        raise Reexecuted
    monkeypatch.setattr(launcher.os, 'execvpe', reexec)
    monkeypatch.setattr(launcher, '__file__', str(receiver))
    monkeypatch.setenv('ZDOTDIR', str(zdot))
    sentinel = tmp_path/'injected'
    arguments = [str(output), 'a space', f'$(touch {sentinel})', '`touch injected`', "single'quote", 'semi;colon', 'line\nbreak']
    with pytest.raises(Reexecuted):
        launcher.reexec_login_shell([*arguments, '--progress-login-shell=zsh'])
    result = subprocess.run(captured['argv'], env=captured['environment'], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    received = json.loads(output.read_text())
    assert received == {'argv':arguments[1:], 'node':'22.19.0'}
    assert not sentinel.exists()
