"""Profile delivery contracts. All credentials and connections are fixtures."""
import hashlib
import io
import json
import sys
from types import SimpleNamespace
from dataclasses import replace
from unittest.mock import patch

import pytest

from scripts import delegation_progress_discord_send as helper

BOT = '1554717538376753244'
THREAD = '1554994350755414139'


def payload(**overrides):
    content = '작업: 진행 중이에요.'
    value = dict(run_id='new-profile-canary', sequence=1, thread_id=THREAD,
                 sender_profile='koharu', expected_bot_id=BOT,
                 operation='CARD_CREATE', event_id='new-profile-canary:card:1',
                 card_receipt=None, content=content,
                 content_digest=hashlib.sha256(content.encode()).hexdigest())
    value.update(overrides)
    return value


class Response:
    def __init__(self, data, status=200):
        self.status, self.data = status, json.dumps(data).encode()

    def read(self, limit):
        return self.data[:limit]


class Discord:
    def __init__(self, bot=BOT):
        self.bot, self.calls, self.messages = bot, [], {}

    def request(self, method, path, body=None, headers=None):
        self.calls.append((method, path, body))
        self.current = method, path, body

    def getresponse(self):
        method, path, body = self.current
        if path == '/api/v10/users/@me':
            return Response(dict(id=self.bot, bot=True))
        if path == '/api/v10/channels/' + THREAD:
            return Response(dict(id=THREAD, type=11))
        if method in ('POST', 'PATCH'):
            message = dict(json.loads(body), id='987654321', channel_id=THREAD,
                           author=dict(id=self.bot, bot=True))
            self.messages['987654321'] = message
            return Response(message)
        return Response(self.messages[path.rsplit('/', 1)[-1]])

    def close(self):
        pass


def test_healthy_koharu_auth_create_patch_and_get_same_card(tmp_path):
    http = Discord()
    value = payload()
    card = helper.deliver(value, {THREAD}, token_loader=lambda: 'FAKE_KOHARU',
                          connection=http, ledger_dir=tmp_path / 'journal')
    assert card['status'] == 'verified'
    assert card['sender_profile'] == 'koharu' and card['bot_id'] == BOT
    patch = payload(sequence=2, operation='CARD_PATCH', event_id='new-profile-canary:card:2',
                    card_receipt=card)
    patched = helper.deliver(patch, {THREAD}, token_loader=lambda: 'FAKE_KOHARU',
                             connection=http, ledger_dir=tmp_path / 'journal')
    assert patched['status'] == 'verified' and patched['message_id'] == card['message_id']
    recovered = helper.deliver(patch, {THREAD}, token_loader=lambda: 'FAKE_KOHARU',
                               connection=http, ledger_dir=tmp_path / 'journal', recover_journal=True)
    assert recovered == patched
    assert [c[0] for c in http.calls].count('POST') == 1
    assert [c[0] for c in http.calls].count('PATCH') == 1
    assert http.calls[:2] == [('GET', '/api/v10/users/@me', None),
                             ('GET', '/api/v10/channels/' + THREAD, None)]


def send(value, tmp_path, http=None, **kwargs):
    return helper.deliver(value, {THREAD}, token_loader=lambda: 'FAKE_SELECTED',
                          connection=http or Discord(), ledger_dir=tmp_path / 'journal', **kwargs)


@pytest.mark.parametrize('profile,bot', [('../koharu', BOT), ('/tmp/koharu', BOT),
    ('other', BOT), ('Koharu', BOT), ('koharu', None), ('koharu', True), ('koharu', '1\n2'),
    (None, BOT), (False, BOT), (['koharu'], BOT)])
def test_invalid_profile_never_loads_or_connects(tmp_path, profile, bot):
    value = payload(sender_profile=profile, expected_bot_id=bot)
    http = Discord()
    def forbidden():
        pytest.fail('credentials must not load')
    result = helper.deliver(value, {THREAD}, token_loader=forbidden, connection=http,
                            ledger_dir=tmp_path / 'journal')
    assert result['status'] == 'rejected' and http.calls == []


@pytest.mark.parametrize('token', [None, '', 'FAKE\nTOKEN', 'FAKE\rTOKEN', 'FAKE TOKEN', '유니코드'])
def test_missing_credential_zero_http_write(tmp_path, token):
    http = Discord()
    result = helper.deliver(payload(), {THREAD}, token_loader=lambda: token,
                            connection=http, ledger_dir=tmp_path / 'journal')
    assert result == dict(status='rejected', reason='missing_credential')
    assert http.calls == []
    assert 'FAKE' not in json.dumps(result)


def test_inherited_or_wrong_bot_fails_before_target_and_write(tmp_path):
    http = Discord(bot='111')
    assert send(payload(), tmp_path, http) == dict(status='rejected', reason='identity_mismatch')
    assert [c[1] for c in http.calls] == ['/api/v10/users/@me']


@pytest.mark.parametrize('profile', ['default', 'koharu'])
@pytest.mark.parametrize('has_key', [True, False])
def test_opaque_canonical_loader_fixed_home_and_scrubs_inheritance(tmp_path, profile, has_key):
    seen = []
    def fake_load(**kwargs):
        assert 'DISCORD_BOT_TOKEN' not in helper.os.environ
        assert 'OTHER_PROFILE_TOKEN' not in helper.os.environ
        assert '_HERMES_KANBAN_EXECUTION_BACKEND' not in helper.os.environ
        assert kwargs['project_env'] is None and kwargs['load_external_secrets'] is False
        seen.append(kwargs['hermes_home'])
        # Simulate a managed fallback even when the selected key is missing.
        helper.os.environ['DISCORD_BOT_TOKEN'] = 'FAKE_SELECTED'
        print('PRIVATE_CREDENTIAL must be silenced')
    def fake_names(path):
        expected_home = tmp_path / '.hermes'
        if profile == 'koharu':
            expected_home /= 'profiles/koharu'
        assert path.parent == expected_home and path.name in ('.env', '.op.env')
        if path.name == '.op.env':
            return set()
        return {'DISCORD_BOT_TOKEN'} if has_key else set()
    env = dict(HOME=str(tmp_path), HERMES_HOME='/wrong/profile', HERMES_PROFILE='wrong',
               DISCORD_BOT_TOKEN='FAKE_INHERITED', OTHER_PROFILE_TOKEN='FAKE_OTHER',
               _HERMES_KANBAN_EXECUTION_BACKEND='fake')
    with patch.dict(helper.os.environ, env, clear=True):
        result = helper._profile_token('/fixture/runtime', profile,
                    canonical_loader=fake_load, key_names=fake_names)
        assert dict(helper.os.environ) == env
    expected = tmp_path / '.hermes'
    if profile == 'koharu':
        expected /= 'profiles/koharu'
    assert seen == [expected]
    assert result == ('FAKE_SELECTED' if has_key else None)


def test_loader_cannot_follow_profile_symlink(tmp_path):
    (tmp_path / '.hermes/profiles').mkdir(parents=True)
    other = tmp_path / 'other'
    other.mkdir()
    (tmp_path / '.hermes/profiles/koharu').symlink_to(other)
    with patch.dict(helper.os.environ, {'HOME': str(tmp_path)}, clear=True):
        with pytest.raises(ValueError, match='profile_mismatch'):
            helper._profile_token('/fixture', 'koharu', canonical_loader=lambda **_: pytest.fail(),
                                  key_names=lambda _: pytest.fail())


@pytest.mark.parametrize('name', ['.env', '.op.env', 'config.yaml'])
def test_loader_cannot_follow_selected_file_symlink(tmp_path, name):
    home = tmp_path / '.hermes/profiles/koharu'
    home.mkdir(parents=True)
    (home / name).symlink_to(tmp_path / 'other-profile-file')
    with patch.dict(helper.os.environ, {'HOME': str(tmp_path)}, clear=True):
        with pytest.raises(ValueError, match='profile_mismatch'):
            helper._profile_token('/fixture', 'koharu', canonical_loader=lambda **_: pytest.fail(),
                                  key_names=lambda _: pytest.fail())


@pytest.mark.parametrize('name', ['.env', '.op.env'])
@pytest.mark.parametrize('selector', ['HERMES_MANAGED_DIR', 'HERMES_HOME', 'HERMES_PROFILE'])
def test_loader_rejects_dotenv_scope_redirect_before_loading(tmp_path, name, selector):
    def names(path):
        return {selector, 'DISCORD_BOT_TOKEN'} if path.name == name else {'DISCORD_BOT_TOKEN'}
    with patch.dict(helper.os.environ, {'HOME': str(tmp_path)}, clear=True):
        with pytest.raises(ValueError, match='profile_mismatch'):
            helper._profile_token('/fixture', 'koharu', canonical_loader=lambda **_: pytest.fail(),
                                  key_names=names)


def test_historical_empty_journal_cannot_acquire_profile_binding(tmp_path):
    journal = tmp_path / 'journal'
    journal.mkdir(mode=0o700)
    path = journal / (payload()['run_id'] + '.json')
    original = json.dumps(dict(records={}, events={}, card=None, last_sequence=0))
    path.write_text(original)
    path.chmod(0o600)
    http = Discord()
    result = send(payload(), tmp_path, http)
    assert result == dict(status='rejected', reason='journal_mismatch')
    assert path.read_text() == original and http.calls == []


def test_legacy_default_loader_preserves_trusted_default_environment(tmp_path, monkeypatch):
    def fake_load():
        assert helper.os.environ['HERMES_HOME'] == str(tmp_path / '.hermes')
    monkeypatch.setitem(sys.modules, 'hermes_cli.send_cmd', SimpleNamespace(_load_hermes_env=fake_load))
    with patch.dict(helper.os.environ, {'HOME': str(tmp_path), 'DISCORD_BOT_TOKEN': 'FAKE_LEGACY'}, clear=True):
        assert helper._default_profile_token('/fixture') == 'FAKE_LEGACY'


def test_production_named_loader_is_read_only_and_restores_hooks(tmp_path, monkeypatch):
    def mutation(*args, **kwargs):
        pytest.fail('credential-only loader must not mutate profile files')
    def fake_load(**kwargs):
        fake_module._sanitize_env_file_if_needed(kwargs['hermes_home'] / '.env')
        fake_module._reapply_terminal_config_bridge(kwargs['hermes_home'])
        fake_module._apply_managed_env()
        helper.os.environ['DISCORD_BOT_TOKEN'] = 'FAKE_SELECTED'
    fake_module = SimpleNamespace(load_hermes_dotenv=fake_load,
        _env_keys_defined_in_dotenv=lambda path: {'DISCORD_BOT_TOKEN'} if path.name == '.env' else set(),
        _sanitize_env_file_if_needed=mutation, _reapply_terminal_config_bridge=mutation,
        _apply_managed_env=mutation)
    monkeypatch.setitem(sys.modules, 'hermes_cli.env_loader', fake_module)
    monkeypatch.setitem(sys.modules, 'hermes_cli.managed_scope', SimpleNamespace(get_managed_dir=lambda: None))
    with patch.dict(helper.os.environ, {'HOME': str(tmp_path)}, clear=True):
        assert helper._profile_token('/fixture', 'koharu') == 'FAKE_SELECTED'
    assert fake_module._sanitize_env_file_if_needed is mutation
    assert fake_module._reapply_terminal_config_bridge is mutation
    assert fake_module._apply_managed_env is mutation


@pytest.mark.parametrize('selector', ["'HERMES_MANAGED_DIR'", 'export\tHERMES_MANAGED_DIR', '\ufeffHERMES_MANAGED_DIR'])
def test_canonical_selector_syntax_cannot_activate_foreign_overlay(tmp_path, monkeypatch, selector):
    from dotenv import dotenv_values
    source = selector + '=/fixture/other\nDISCORD_BOT_TOKEN=FAKE_SELECTED\n'
    def forbidden_overlay():
        pytest.fail('canonical managed overlay must not read another home')
    def fake_load(**kwargs):
        helper.os.environ.update(dotenv_values(stream=io.StringIO(source.lstrip('\ufeff'))))
        fake_module._apply_managed_env()
    fake_module = SimpleNamespace(load_hermes_dotenv=fake_load,
        _env_keys_defined_in_dotenv=lambda path: {selector, 'DISCORD_BOT_TOKEN'} if path.name == '.env' else set(),
        _sanitize_env_file_if_needed=lambda *_: None, _reapply_terminal_config_bridge=lambda *_: None,
        _apply_managed_env=forbidden_overlay)
    monkeypatch.setitem(sys.modules, 'hermes_cli.env_loader', fake_module)
    monkeypatch.setitem(sys.modules, 'hermes_cli.managed_scope', SimpleNamespace(get_managed_dir=lambda: None))
    original = {'HOME': str(tmp_path), 'DISCORD_BOT_TOKEN': 'FAKE_INHERITED'}
    with patch.dict(helper.os.environ, original, clear=True):
        with pytest.raises(ValueError, match='profile_mismatch'):
            helper._profile_token('/fixture', 'koharu')
        assert dict(helper.os.environ) == original
    assert fake_module._apply_managed_env is forbidden_overlay


class FaultDiscord(Discord):
    def __init__(self, mutate, **kwargs):
        super().__init__(**kwargs)
        self.mutate = mutate

    def getresponse(self):
        return self.mutate(self, super().getresponse())


@pytest.mark.parametrize('stage', ['identity', 'target', 'write', 'readback'])
@pytest.mark.parametrize('status,reason', [(401, 'authentication_failed'), (403, 'target_access_denied'),
    (404, 'target_not_found'), (429, 'rate_limited'), (500, 'remote_unavailable')])
def test_authenticated_errors_keep_safe_semantics(tmp_path, stage, status, reason):
    def mutate(http, response):
        method, path, _ = http.current
        current = ('identity' if path.endswith('/users/@me') else
                   'target' if path.endswith('/' + THREAD) else
                   'write' if method == 'POST' else 'readback')
        if current == stage:
            return Response(dict(code=50013, message='@everyone https://user:PRIVATE_SECRET@evil'), status)
        return response
    http = FaultDiscord(mutate)
    result = send(payload(), tmp_path, http)
    expected_status = 'uncertain' if stage == 'readback' or status == 500 else 'rejected'
    assert result == dict(status=expected_status, reason=reason, http_status=status, discord_code=50013)
    if stage in ('identity', 'target'):
        assert not any(c[0] != 'GET' for c in http.calls)
    assert 'PRIVATE_SECRET' not in json.dumps(result)


@pytest.mark.parametrize('code', [True, '50013 PRIVATE_SECRET', -1, 1000001, {'secret': 'PRIVATE_SECRET'}])
def test_remote_error_codes_are_numeric_and_bounded(tmp_path, code):
    http = FaultDiscord(lambda _, __: Response({'code': code, 'message': 'PRIVATE_SECRET'}, 403))
    assert send(payload(), tmp_path, http) == dict(status='rejected', reason='target_access_denied', http_status=403)


def test_patch_requires_actual_creator_before_mutation(tmp_path):
    http = Discord()
    card = send(payload(), tmp_path, http)
    http.messages[card['message_id']]['author']['id'] = '111'
    value = payload(sequence=2, operation='CARD_PATCH', event_id='new-profile-canary:card:2', card_receipt=card)
    assert send(value, tmp_path, http) == dict(status='rejected', reason='author_mismatch')
    assert not any(c[0] == 'PATCH' for c in http.calls)


def test_get_author_mismatch_after_post_is_uncertain(tmp_path):
    def mutate(http, response):
        if http.current[0] == 'GET' and '/messages/' in http.current[1]:
            data = json.loads(response.data)
            data['author']['id'] = '111'
            return Response(data)
        return response
    http = FaultDiscord(mutate)
    assert send(payload(), tmp_path, http) == dict(status='uncertain', reason='readback_mismatch')
    assert [c[0] for c in http.calls].count('POST') == 1


@pytest.mark.parametrize('field,bad', [('sender_profile', 'default'), ('expected_bot_id', '111'),
    ('bot_id', '111'), ('thread_id', '111'), ('run_id', 'another-run'), ('sequence', True),
    ('message_id', 'not-numeric'), ('extra', 'PRIVATE_SECRET')])
def test_receipt_cross_identity_fails(field, bad):
    value = payload()
    card = helper.receipt(value, '987654321', bot_id=BOT)
    card[field] = bad
    with pytest.raises(ValueError):
        helper.validate_receipt(card, value)
    patch_value = payload(sequence=2, operation='CARD_PATCH', event_id='new-profile-canary:card:2', card_receipt=card)
    with pytest.raises((ValueError, TypeError)):
        helper.validate(patch_value, {THREAD})


@pytest.mark.parametrize('field,bad', [('sender_profile', 'default'), ('expected_bot_id', '111'),
    ('thread_id', '111'), ('run_id', 'another-run')])
def test_journal_binding_mismatch_never_writes_or_rebinds(tmp_path, field, bad):
    value = payload()
    assert send(value, tmp_path)['status'] == 'verified'
    path = tmp_path / 'journal' / (value['run_id'] + '.json')
    state = json.loads(path.read_text())
    state['binding'][field] = bad
    helper._save(path, state)
    before = path.read_bytes()
    http = Discord()
    assert send(value, tmp_path, http)['reason'] == 'journal_mismatch'
    assert http.calls == [] and path.read_bytes() == before


def test_same_run_across_profiles_and_legacy_journals_never_rebind(tmp_path):
    value = payload()
    assert send(value, tmp_path)['status'] == 'verified'
    path = tmp_path / 'journal' / (value['run_id'] + '.json')
    before = path.read_bytes()
    http = Discord()
    other = payload(sender_profile='default')
    assert send(other, tmp_path, http)['reason'] == 'journal_mismatch'
    assert http.calls == [] and path.read_bytes() == before
    # Also refuse converting an old failed default journal to koharu.
    old = dict(thread_id=THREAD, records={'1': {'digest': 'legacy', 'result': {'status': 'rejected'},
                'known_message': None}}, events={}, card=None, last_sequence=0)
    helper._save(path, old)
    before = path.read_bytes()
    assert send(value, tmp_path, http)['reason'] == 'journal_mismatch'
    assert http.calls == [] and path.read_bytes() == before


@pytest.mark.parametrize('lost_at', ['POST', 'readback'])
def test_ambiguous_response_and_restart_never_duplicate_create(tmp_path, lost_at):
    lost = [False]
    def mutate(http, response):
        method, path, _ = http.current
        if not lost[0] and (method == lost_at or lost_at == 'readback' and '/messages/' in path):
            lost[0] = True
            raise TimeoutError('PRIVATE_EXCEPTION_TOKEN')
        return response
    http = FaultDiscord(mutate)
    value = payload()
    assert send(value, tmp_path, http)['status'] == 'uncertain'
    calls = len(http.calls)
    assert send(value, tmp_path, http)['status'] == 'uncertain'
    assert len(http.calls) == calls
    recovered = send(value, tmp_path, http, recover_journal=True)
    assert recovered['status'] == ('verified' if lost_at == 'readback' else 'uncertain')
    assert [c[0] for c in http.calls].count('POST') == 1
    assert all(c[0] == 'GET' for c in http.calls[calls:])
    assert 'PRIVATE_EXCEPTION_TOKEN' not in json.dumps(recovered)


def test_failed_recovery_keeps_uncertainty_even_after_auth_repair(tmp_path):
    fail_get = [True]
    def mutate(http, response):
        if fail_get[0] and '/messages/' in http.current[1]:
            return Response({'code': 50013}, 403)
        return response
    http = FaultDiscord(mutate)
    value = payload()
    assert send(value, tmp_path, http)['status'] == 'uncertain'
    http.bot = '111'
    assert send(value, tmp_path, http, recover_journal=True)['status'] == 'uncertain'
    http.bot, fail_get[0] = BOT, False
    assert send(value, tmp_path, http)['status'] == 'uncertain'
    assert send(value, tmp_path, http, recover_journal=True)['status'] == 'verified'
    assert [c[0] for c in http.calls].count('POST') == 1


@pytest.mark.parametrize('text', ['@everyone', '<@123>', '**markup**', 'https://user:SECRET@evil',
    'sk-' + 'a' * 40, 'x\x1b[2J', 'MEDIA:/tmp/a'])
def test_unsafe_content_rejected_without_auth(tmp_path, text):
    value = payload(content=text, content_digest=hashlib.sha256(text.encode()).hexdigest())
    def forbidden():
        pytest.fail('credential loader must not run')
    http = Discord()
    assert helper.deliver(value, {THREAD}, token_loader=forbidden, connection=http,
                          ledger_dir=tmp_path / 'journal')['status'] == 'rejected'
    assert http.calls == []


def test_default_explicit_without_expected_id_pins_observed_bot(tmp_path):
    value = payload(sender_profile='default', expected_bot_id=None)
    http = Discord(bot='111')
    card = send(value, tmp_path, http)
    assert card['bot_id'] == '111'
    http.bot = BOT
    patch_value = dict(value, sequence=2, operation='CARD_PATCH', event_id=value['run_id'] + ':card:2', card_receipt=card)
    assert send(patch_value, tmp_path, http)['reason'] == 'identity_mismatch'
    assert [c[0] for c in http.calls].count('PATCH') == 0


def test_manifest_outbox_and_local_state_bind_profiles(tmp_path):
    from tests.agent.test_delegation_progress_v2_red import lane
    from agent.delegation_progress import Progress, _atomic
    from agent.delegation_progress_delivery import Delivery
    p = lane(tmp_path)
    legacy_binding = p.manifest.binding()
    p.manifest = replace(p.manifest, sender_profile='koharu', expected_bot_id=BOT)
    assert p.manifest.binding() != legacy_binding
    message = p.tick(now=0)['queued'][0]
    assert message['sender_profile'] == 'koharu' and message['expected_bot_id'] == BOT
    with pytest.raises(ValueError, match='state_identity'):
        Progress(replace(p.manifest, sender_profile='default'), p.root).peek()
    state = p._load()
    state['pending'][0]['sender_profile'] = 'default'
    _atomic(p.path, state)
    with pytest.raises(ValueError, match='outbox_identity'):
        Delivery(p, None).drain_one()


def test_legacy_default_create_patch_get_positive_control(tmp_path):
    http = Discord()
    create = {k: v for k, v in payload().items() if k not in ('sender_profile', 'expected_bot_id')}
    first = helper.deliver(create, {create['thread_id']}, token_loader=lambda: 'FAKE_DEFAULT',
                           connection=http, ledger_dir=tmp_path / 'journal')
    assert first['status'] == 'verified' and 'sender_profile' not in first
    update = dict(create, sequence=2, operation='CARD_PATCH', event_id=create['run_id'] + ':card:2', card_receipt=first)
    second = helper.deliver(update, {update['thread_id']}, token_loader=lambda: 'FAKE_DEFAULT',
                            connection=http, ledger_dir=tmp_path / 'journal')
    assert second['status'] == 'verified' and second['message_id'] == first['message_id']
    assert helper.deliver(update, {update['thread_id']}, token_loader=lambda: 'FAKE_DEFAULT',
                          connection=http, ledger_dir=tmp_path / 'journal', recover_journal=True) == second
    assert [c[0] for c in http.calls].count('POST') == 1
