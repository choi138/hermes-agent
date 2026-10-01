"""Actual CLI parser and SSH argv; offline and credential-free."""
import json
import shlex
import io
import sys
import subprocess
from contextlib import redirect_stdout
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.delegation_progress_delivery import SSHSender
from tests.agent.test_profile_progress_delivery import BOT, THREAD, payload, Discord


def test_profile_sender_round_trip_to_helper():
    from scripts.delegation_progress_discord_send import receipt
    seen = []
    def transport(argv, raw, timeout):
        seen.append((shlex.split(argv[-1]), json.loads(raw)))
        return json.dumps(receipt(json.loads(raw), '987654321', bot_id=BOT)).encode()
    sender = SSHSender('fixture-host', '/usr/bin/python3', '/srv/hermes', '/srv/helper.py',
                       [THREAD], sender_profile='koharu', expected_bot_id=BOT,
                       server_state_dir='/srv/journal', transport=transport)
    assert sender.send(payload())['status'] == 'verified'
    assert seen[0][1] == payload()
    assert seen[0][0][-4:] == ['--sender-profile', 'koharu', '--expected-bot-id', BOT]


def helper_input(monkeypatch, value):
    from scripts import delegation_progress_discord_send as helper
    monkeypatch.setattr(helper.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(value).encode())))


@pytest.mark.parametrize('profile', [None, 'default', 'koharu'])
def test_helper_dry_run_has_no_auth_or_http(monkeypatch, capsys, profile):
    from scripts import delegation_progress_discord_send as helper
    def forbidden(*_, **__):
        pytest.fail('dry-run must not load auth or connect')
    monkeypatch.setattr(helper, '_profile_token', forbidden)
    monkeypatch.setattr(helper, '_default_profile_token', forbidden)
    monkeypatch.setattr(helper.http.client, 'HTTPSConnection', forbidden)
    value, argv = payload(), ['--runtime-root', '/fixture/runtime', '--allow-thread', THREAD, '--dry-run']
    if profile is None:
        value.pop('sender_profile')
        value.pop('expected_bot_id')
    else:
        value['sender_profile'] = profile
        argv += ['--sender-profile', profile, '--expected-bot-id', BOT]
    helper_input(monkeypatch, value)
    assert helper.main(argv) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'validated'


@pytest.mark.parametrize('args', [[], ['--sender-profile', 'default', '--expected-bot-id', BOT],
    ['--sender-profile', 'koharu'], ['--sender-profile', 'koharu', '--expected-bot-id', '1\n2']])
def test_helper_cli_identity_mismatch_no_auth(monkeypatch, capsys, args):
    from scripts import delegation_progress_discord_send as helper
    monkeypatch.setattr(helper, '_profile_token', lambda *_: pytest.fail('no auth on mismatch'))
    monkeypatch.setattr(helper, '_default_profile_token', lambda *_: pytest.fail('no default fallback'))
    helper_input(monkeypatch, payload())
    assert helper.main(['--runtime-root', '/fixture/runtime', '--allow-thread', THREAD, *args]) == 75
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'rejected' and result['reason'] in ('invalid_payload', 'profile_mismatch')


@pytest.mark.parametrize('script', ['delegation_progress_discord_send.py', 'delegation_progress_bridge.py',
    'run_codex_task.py', 'delegation_progress_supervisor.py', 'delegation_progress.py'])
def test_actual_subprocess_help_does_not_load_runtime(script):
    path = Path(__file__).resolve().parents[2] / 'scripts' / script
    completed = subprocess.run([sys.executable, '-I', str(path), '--help'],
                                capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    assert 'usage:' in completed.stdout


def registered_lane(tmp_path):
    from tests.agent.test_delegation_progress_v2_red import lane
    from agent.delegation_progress import _atomic
    p = lane(tmp_path)
    p.manifest = replace(p.manifest, thread_id=THREAD, sender_profile='koharu', expected_bot_id=BOT)
    p.tick()
    manifest_path = tmp_path / 'manifest.json'
    _atomic(manifest_path, {k: str(v) if isinstance(v, Path) else v for k, v in asdict(p.manifest).items()})
    return p, manifest_path


def bridge_args(p, manifest):
    return ['--manifest', str(manifest), '--state-dir', str(p.root), '--ssh-host', 'fixture-host',
            '--remote-python', '/usr/bin/python3', '--runtime-root', '/fixture/runtime',
            '--helper-path', '/fixture/helper.py', '--allow-thread', THREAD,
            '--server-state-dir', '/fixture/journal', '--sender-profile', 'koharu', '--expected-bot-id', BOT]


def test_actual_bridge_dryrun_and_malformed_trusted_args_no_egress(tmp_path, monkeypatch, capsys):
    from scripts.delegation_progress_bridge import main
    p, manifest = registered_lane(tmp_path)
    before = p.path.read_bytes()
    monkeypatch.setattr(SSHSender, 'send', lambda *_, **__: pytest.fail('no SSH in dry-run'))
    args = bridge_args(p, manifest)
    assert main([*args, '--dry-run']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'validated'
    assert p.path.read_bytes() == before
    args[args.index('koharu')] = 'default'
    assert main([*args, '--dry-run']) == 78
    assert json.loads(capsys.readouterr().out)['reason'] == 'profile_mismatch'


@pytest.mark.parametrize('token', [None, 'FAKE_KOHARU'])
def test_actual_helper_preflight_get_only_no_journal(tmp_path, monkeypatch, capsys, token):
    from scripts import delegation_progress_discord_send as helper
    http, selected = Discord(), []
    monkeypatch.setattr(helper, '_profile_token', lambda root, profile: selected.append((root, profile)) or token)
    monkeypatch.setattr(helper.http.client, 'HTTPSConnection', lambda *_, **__: http)
    helper_input(monkeypatch, payload())
    journal = tmp_path / 'must-not-exist'
    code = helper.main(['--runtime-root', '/fixture/runtime', '--allow-thread', THREAD,
        '--sender-profile', 'koharu', '--expected-bot-id', BOT, '--preflight', '--delivery-state-dir', str(journal)])
    result = json.loads(capsys.readouterr().out)
    assert selected == [('/fixture/runtime', 'koharu')]
    assert code == (75 if token is None else 0)
    assert result['status'] == ('rejected' if token is None else 'ready')
    assert not journal.exists() and all(c[0] == 'GET' for c in http.calls)


def test_actual_manifest_bridge_transport_helper_create_patch_readback(tmp_path, monkeypatch):
    from scripts import delegation_progress_discord_send as helper
    from scripts.delegation_progress_bridge import main
    from agent import delegation_progress_delivery as delivery
    p, manifest = registered_lane(tmp_path)
    http, payloads = Discord(), []
    monkeypatch.setattr(helper, '_profile_token', lambda root, profile: 'FAKE_KOHARU' if profile == 'koharu' else pytest.fail())
    monkeypatch.setattr(helper.http.client, 'HTTPSConnection', lambda *_, **__: http)
    def transport(argv, raw, timeout):
        remote = shlex.split(argv[-1])[2:]
        remote[remote.index('--delivery-state-dir') + 1] = str(tmp_path / 'server-journal')
        monkeypatch.setattr(helper.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
        payloads.append(json.loads(raw))
        output = io.StringIO()
        with redirect_stdout(output):
            assert helper.main(remote) == 0
        return output.getvalue().encode()
    real_sender = delivery.SSHSender
    monkeypatch.setattr(delivery, 'SSHSender', lambda *a, **kw: real_sender(*a, **kw, transport=transport))
    args = bridge_args(p, manifest)
    assert main([*args, '--preflight']) == 0
    assert p.peek()['operation'] == 'CARD_CREATE'  # preflight never ACKs or writes.
    assert main([*args, '--once']) == 0
    (p.manifest.worktree / 'a.py').write_text('value = 2\n')
    p.tick(now=p._load()['last_card_at'] + 31)
    assert main([*args, '--once']) == 0
    assert [v['operation'] for v in payloads] == ['CARD_CREATE', 'CARD_CREATE', 'CARD_PATCH']
    assert all(v['sender_profile'] == 'koharu' and v['expected_bot_id'] == BOT for v in payloads)
    assert [c[0] for c in http.calls].count('POST') == 1
    assert [c[0] for c in http.calls].count('PATCH') == 1
    patch_value = payloads[-1]
    assert helper.deliver(patch_value, {THREAD}, token_loader=lambda: 'FAKE_KOHARU', connection=http,
                          ledger_dir=tmp_path / 'server-journal', recover_journal=True)['status'] == 'verified'
    assert [c[0] for c in http.calls].count('POST') == 1


@pytest.mark.parametrize('profile', [None, 'koharu'])
def test_launcher_parser_registration_forwarding_and_policy_unchanged(tmp_path, monkeypatch, capsys, profile):
    from scripts import run_codex_task as cli
    from agent import codex_task_runner as runner
    from agent.delegation_progress import Manifest
    from tests.agent.test_delegation_progress_v2_red import lane
    p = lane(tmp_path)
    spec = p.manifest.worktree / 'SPEC.md'
    spec.write_text('fixture bounded task')
    manifest = tmp_path / 'launch-manifest.json'
    args = ['run_codex_task.py', '--spec', str(spec), '--workdir', str(p.manifest.worktree),
            '--allowed-root', str(tmp_path), '--output-dir', str(p.manifest.artifact_root),
            '--progress-manifest', str(manifest), '--progress-state-dir', str(p.root),
            '--progress-thread', THREAD, '--progress-label', 'fixture', '--task-class', 'bounded']
    if profile:
        args += ['--progress-sender-profile', profile, '--progress-expected-bot-id', BOT]
    monkeypatch.setattr(cli.sys, 'argv', [*args, '--dry-run'])
    assert cli.main() == 0
    dry = json.loads(capsys.readouterr().out)
    assert not manifest.exists() and not p.root.exists()
    def fake_run(request, *, before_spawn, policy_receipt):
        artifact = request.output_dir / 'fake-run'
        artifact.mkdir(mode=0o700)
        (artifact / 'events.jsonl').touch()  # Real runner opens this before registration.
        before_spawn(request, artifact)  # Real registration; no worker/inference.
        result = request.inspect(policy_receipt)
        result['exit_code'] = 0
        return result
    monkeypatch.setattr(runner, 'run_task', fake_run)
    monkeypatch.setattr(cli.sys, 'argv', args)
    assert cli.main() == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    observed = Manifest.load(manifest)
    assert observed.sender_profile == profile and observed.expected_bot_id == (BOT if profile else None)
    for result in (dry, lines[-1]):
        assert result['configuration']['requested'] == result['configuration']['serialized'] == dry['policy']['selected']
    assert dry['policy']['selected'] == {'model': 'gpt-6-luna', 'effort': 'medium'}


def test_supervisor_rejection_stops_on_first_attempt_and_keeps_pending(tmp_path):
    from scripts.delegation_progress_supervisor import supervise
    from tests.agent.test_delegation_progress_v3 import supervisor_config
    p, _ = registered_lane(tmp_path)
    config = supervisor_config(p, tmp_path)
    calls = []
    assert supervise(config, run_bridge=lambda _: calls.append(True) or 78,
                     sleep=lambda _: pytest.fail('rejected profile must not retry')) == 0
    assert len(calls) == 1 and p.peek() is not None
    assert json.loads((p.directory / 'supervision.json').read_text())['status'] == 'attention'


def test_supervisor_preflight_before_install(tmp_path, monkeypatch):
    from scripts import delegation_progress_supervisor as supervisor
    p, manifest = registered_lane(tmp_path)
    calls = []
    def fake_bridge(argv):
        calls.append(argv)
        return 0 if '--dry-run' in argv else 78
    monkeypatch.setattr(supervisor, 'bridge_main', fake_bridge)
    monkeypatch.setattr(supervisor.subprocess, 'run', lambda *_, **__: pytest.fail('must not install supervisor'))
    with pytest.raises(ValueError, match='profile_preflight_failed'):
        supervisor.launch(bridge_args(p, manifest), wait=False)
    assert '--dry-run' in calls[0] and '--preflight' in calls[1] and '--recover-journal' not in calls[1]
    assert not (p.directory / 'supervisor-config.json').exists()
