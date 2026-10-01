#!/usr/bin/env python3
"""Opt-in server helper. Stages as ONE stdlib-only file beside a Hermes runtime.

Only main's non-dry-run path loads the selected fixed profile credential. No import
side effects, redirects, attachments, channel lookup or arbitrary endpoint.
"""
import argparse
from contextlib import redirect_stdout, redirect_stderr, contextmanager
import fcntl
import stat
import tempfile
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import signal
import sys

MAX_INPUT = 16384
MAX_RESPONSE = 65536
MENTIONS = {"parse": [], "replied_user": False}
REASONS = frozenset(('missing_credential', 'profile_mismatch', 'identity_mismatch',
    'target_access_denied', 'invalid_payload', 'authentication_failed', 'target_not_found',
    'rate_limited', 'request_rejected', 'remote_unavailable', 'transport_error',
    'readback_mismatch', 'author_mismatch', 'journal_mismatch', 'recovery_unbound'))


def validate_profile(sender_profile, expected_bot_id):
    """None is the legacy default contract; names are trusted CLI authority only."""
    if sender_profile is None:
        if expected_bot_id is not None:
            raise ValueError('profile_mismatch')
    elif (not isinstance(sender_profile, str) or sender_profile not in ('default', 'koharu')
          or expected_bot_id is not None and not identifier(expected_bot_id)
          or sender_profile == 'koharu' and expected_bot_id is None):
        raise ValueError('profile_mismatch')


def profile_fields(value):
    return {k: value[k] for k in ('sender_profile', 'expected_bot_id') if k in value}


def failure(value, status, reason, *, http_status=None, discord_code=None):
    # Existing in-flight traffic retains its exact result schema.
    result = {'status': status}
    if isinstance(value, dict) and 'sender_profile' in value:
        result['reason'] = reason
        if type(http_status) is int and 100 <= http_status <= 599:
            result['http_status'] = http_status
        if type(discord_code) is int and 0 <= discord_code <= 1000000:
            result['discord_code'] = discord_code
    return result


def validate_failure(result):
    if (not isinstance(result, dict) or result.get('status') not in ('rejected', 'uncertain')
            or set(result) - {'status', 'reason', 'http_status', 'discord_code'}
            or 'reason' in result and result['reason'] not in REASONS
            or set(result) != {'status'} and 'reason' not in result
            or 'http_status' in result and (type(result['http_status']) is not int or not 100 <= result['http_status'] <= 599)
            or 'discord_code' in result and (type(result['discord_code']) is not int or not 0 <= result['discord_code'] <= 1000000)):
        raise ValueError('failure_schema')
    return result


def nonce(value):
    identity = value['run_id'] + ':' + str(value['sequence'])
    if 'sender_profile' in value:
        identity = json.dumps([value['sender_profile'], value['expected_bot_id'], value['thread_id'], identity])
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9]{1,22}", value) is not None


def decode(raw):
    if len(raw) > MAX_INPUT:
        raise ValueError('input_limit')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def validate(value, allow_threads):
    base = {'run_id', 'sequence', 'thread_id', 'content', 'content_digest'}
    v2 = isinstance(value, dict) and 'operation' in value
    expected = base | {'operation', 'event_id', 'card_receipt'} if v2 else base
    if isinstance(value, dict) and 'sender_profile' in value:
        expected |= {'sender_profile', 'expected_bot_id'}
        if not v2 or value['sender_profile'] is None:
            raise ValueError('profile_mismatch')
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError('payload_schema')
    validate_profile(value.get('sender_profile'), value.get('expected_bot_id'))
    if (not isinstance(value['run_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value['run_id'])
            or type(value['sequence']) is not int or not 1 <= value['sequence'] <= 1000000
            or not identifier(value['thread_id']) or value['thread_id'] not in allow_threads):
        raise ValueError('payload_identity')
    content = value['content']
    if v2:
        if (value['operation'] not in ('CARD_CREATE', 'CARD_PATCH', 'NOTICE') or
                not isinstance(value['event_id'], str) or not re.fullmatch(
                    re.escape(value['run_id']) + r':[A-Za-z0-9:_-]{1,100}', value['event_id'])):
            raise ValueError('operation_identity')
        legacy = r'\*\*[가-힣A-Za-z0-9 -]{1,64} · [가-힣 ]{1,32}\*\*\n• 변경: [가-힣A-Za-z0-9 ·.,-]+\n• 검증: [가-힣A-Za-z0-9 ·.,-]+\n• 남음: [가-힣A-Za-z0-9 ·.,-]+'
        conversational = r'[가-힣A-Za-z0-9 -]{1,64}: [가-힣A-Za-z0-9 ·.,-]+(?:\n[가-힣A-Za-z0-9 ·.,-]+)?'
        if (not isinstance(content, str) or len(content) > 900 or not (
                re.fullmatch(legacy, content) or re.fullmatch(conversational, content))):
            raise ValueError('unsafe_content')
        card = value['card_receipt']
        if value['operation'] == 'CARD_PATCH':
            if (not isinstance(card, dict) or card.get('operation') != 'CARD_CREATE' or
                    card.get('run_id') != value['run_id'] or card.get('thread_id') != value['thread_id'] or
                    type(card.get('sequence')) is not int or card['sequence'] >= value['sequence'] or
                    not identifier(card.get('message_id')) or card.get('status') != 'verified'):
                raise ValueError('card_binding')
            if profile_fields(card) != profile_fields(value):
                raise ValueError('card_binding')
            if 'sender_profile' in value:
                # Validate the complete creator receipt, including observed bot.
                creator = dict(value, sequence=card['sequence'], operation='CARD_CREATE',
                               event_id=card.get('event_id'), content_digest=card.get('content_digest'),
                               card_receipt=None)
                if (not isinstance(card.get('event_id'), str) or not re.fullmatch(
                        re.escape(value['run_id']) + r':[A-Za-z0-9:_-]{1,100}', card['event_id'])
                        or not isinstance(card.get('content_digest'), str)
                        or not re.fullmatch(r'[a-f0-9]{64}', card['content_digest'])
                        or card['sequence'] < 1):
                    raise ValueError('card_binding')
                validate_receipt(card, creator)
        elif card is not None:
            raise ValueError('unexpected_card')
    elif (not isinstance(content, str) or not 1 <= len(content) <= 1200
            or not content.startswith('작업 진행 상황을 전해드려요.\n')
            or not re.fullmatch(r'[가-힣A-Za-z0-9\n •·.,:/_\\()%\-]+', content)
            or re.search(r'\\(?!_)', content)):
        raise ValueError('unsafe_content')
    if (re.search(r'MEDIA|https?\s*:|www\.|(?:sk|ghp|github_pat|xox[baprs])[-_]|[A-Za-z0-9]{24,}', content, re.I)
            or value['content_digest'] != hashlib.sha256(content.encode()).hexdigest()):
        raise ValueError('unsafe_content')
    return value


def receipt(value, message_id, *, bot_id=None):
    result = {'status': 'verified', 'run_id': value['run_id'], 'sequence': value['sequence'],
            'thread_id': value['thread_id'], 'content_digest': value['content_digest'], 'message_id': message_id}
    if 'operation' in value:
        result.update(operation=value['operation'], event_id=value['event_id'])
    if 'sender_profile' in value:
        if not identifier(bot_id) or value['expected_bot_id'] is not None and bot_id != value['expected_bot_id']:
            raise ValueError('identity_mismatch')
        result.update(**profile_fields(value), bot_id=bot_id)
    return result


def validate_receipt(result, value):
    if (not isinstance(result, dict) or not identifier(result.get('message_id'))
            or type(result.get('sequence')) is not int):
        raise ValueError('receipt_schema')
    if result != receipt(value, result['message_id'], bot_id=result.get('bot_id')):
        raise ValueError('receipt_identity')
    if value.get('operation') == 'CARD_PATCH' and result['message_id'] != value['card_receipt']['message_id']:
        raise ValueError('patch_target_identity')
    if 'sender_profile' in value and value.get('operation') == 'CARD_PATCH' and result['bot_id'] != value['card_receipt']['bot_id']:
        raise ValueError('identity_mismatch')
    return result


def discord_body(value):
    body = {'content': value['content'], 'allowed_mentions': MENTIONS}
    if 'operation' in value and value['operation'] != 'CARD_PATCH':
        body.update(nonce=nonce(value), enforce_nonce=True)
    return body


def _deliver(value, allow_threads, *, token_loader, connection=None, reconcile_message=None,
             on_response=None, response_bound=False, on_identity=None, pinned_bot_id=None,
             preflight=False):
    """HTTP injection is fixture-only. Profile-bound traffic is identity checked."""
    try:
        validate(value, allow_threads)
        if reconcile_message is not None and not identifier(reconcile_message):
            raise ValueError('message_identity')
    except (ValueError, TypeError):
        return failure(value, 'rejected', 'invalid_payload')
    if reconcile_message is not None and not response_bound:
        return failure(value, 'uncertain', 'recovery_unbound')
    attempted = False
    bound = 'sender_profile' in value
    bot_id = None
    try:
        token = token_loader()
        if not isinstance(token, str) or not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            return failure(value, 'rejected', 'missing_credential')
        headers = {'Authorization': 'Bot ' + token, 'Content-Type': 'application/json',
                   'User-Agent': 'HermesProgressBridge/1.0'}
        conn = connection or http.client.HTTPSConnection('discord.com', timeout=10)
        base = '/api/v10/channels/' + value['thread_id'] + '/messages'

        def read():
            response = conn.getresponse()
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError('response_limit')
            # The status stays meaningful even when Discord sends a non-JSON error.
            try:
                data = decode(raw)
            except (ValueError, UnicodeError):
                data = None
            return response.status, data

        def http_failure(status, data, *, uncertain=False):
            reason = {401: 'authentication_failed', 403: 'target_access_denied',
                      404: 'target_not_found', 429: 'rate_limited'}.get(status,
                      'remote_unavailable' if status >= 500 else 'request_rejected')
            certainty = 'uncertain' if uncertain or status >= 500 else 'rejected'
            return failure(value, certainty, reason, http_status=status,
                           discord_code=data.get('code') if isinstance(data, dict) else None)

        if bound:
            conn.request('GET', '/api/v10/users/@me', headers=headers)
            status, me = read()
            if status != 200:
                return http_failure(status, me)
            if (not isinstance(me, dict) or not identifier(me.get('id')) or me.get('bot') is not True
                    or value['expected_bot_id'] is not None and me['id'] != value['expected_bot_id']
                    or pinned_bot_id is not None and me['id'] != pinned_bot_id):
                return failure(value, 'rejected', 'identity_mismatch')
            bot_id = me['id']
            conn.request('GET', '/api/v10/channels/' + value['thread_id'], headers=headers)
            status, channel = read()
            if status != 200:
                return http_failure(status, channel)
            if not isinstance(channel, dict) or channel.get('id') != value['thread_id']:
                return failure(value, 'rejected', 'target_access_denied')
            if preflight:
                return dict(status='ready', run_id=value['run_id'], thread_id=value['thread_id'],
                            **profile_fields(value), bot_id=bot_id)
            if on_identity:
                on_identity(bot_id)  # Freeze and fsync identity before any write.
        elif preflight:
            return failure(value, 'rejected', 'profile_mismatch')

        message_id = reconcile_message
        target = value.get('card_receipt', {}).get('message_id') if value.get('card_receipt') else None
        if target and message_id and message_id != target:
            return failure(value, 'rejected', 'invalid_payload')
        if bound and target and message_id is None:
            # Creator-only PATCH: author must be checked BEFORE mutation.
            conn.request('GET', base + '/' + target, headers=headers)
            status, current = read()
            if status != 200:
                return http_failure(status, current)
            if (not isinstance(current, dict) or current.get('id') != target
                    or current.get('channel_id') != value['thread_id']
                    or not isinstance(current.get('author'), dict)
                    or current['author'].get('id') != bot_id
                    or value['card_receipt']['bot_id'] != bot_id):
                return failure(value, 'rejected', 'author_mismatch')
        if message_id is None:
            attempted = True  # From request onward, a write may have been accepted.
            conn.request('PATCH' if target else 'POST', base + '/' + target if target else base,
                         body=json.dumps(discord_body(value), ensure_ascii=True).encode(), headers=headers)
            status, posted = read()
            if status in (400, 401, 403, 404, 405, 413, 429):
                return http_failure(status, posted)
            if status not in (200, 201) or not isinstance(posted, dict):
                return http_failure(status, posted, uncertain=True)
            message_id = posted.get('id')
            if (not identifier(message_id) or target and message_id != target
                    or posted.get('channel_id') != value['thread_id'] or posted.get('content') != value['content']
                    or 'operation' in value and not target and 'nonce' in posted and posted['nonce'] != nonce(value)
                    or bound and (not isinstance(posted.get('author'), dict) or posted['author'].get('id') != bot_id)):
                return failure(value, 'uncertain', 'readback_mismatch')
            if on_response:
                on_response(message_id)
        attempted = True
        conn.request('GET', base + '/' + message_id, headers=headers)
        status, actual = read()
        if status != 200:
            return http_failure(status, actual, uncertain=True)
        if (not isinstance(actual, dict) or actual.get('id') != message_id
                or actual.get('channel_id') != value['thread_id'] or actual.get('content') != value['content']
                or bound and (not isinstance(actual.get('author'), dict) or actual['author'].get('id') != bot_id)):
            return failure(value, 'uncertain', 'readback_mismatch')
        return receipt(value, message_id, bot_id=bot_id)
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) and str(exc) in ('profile_mismatch', 'identity_mismatch') else 'transport_error'
        return failure(value, 'uncertain' if attempted else 'rejected', reason)
    finally:
        if 'conn' in locals():
            try:
                conn.close()
            except Exception:
                pass


def _save(path, value):
    encoded = json.dumps(value, sort_keys=True)
    if len(encoded) > 16 * 1024 * 1024:
        raise ValueError('journal_limit')
    fd, name = tempfile.mkstemp(prefix='.receipt-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def _journal(root, run_id):
    root = Path(root)
    if not root.is_absolute() or root.resolve() != root:
        raise ValueError('journal_path')
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('private_journal_required')
    fd = os.open(root / (run_id + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('journal_lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = root / (run_id + '.json')
        fresh = False
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16 * 1024 * 1024
                        or info.st_uid != os.getuid() or info.st_mode & 0o077):
                    raise ValueError('journal_file')
                state = json.load(stream)
        except FileNotFoundError:
            state = {'records': {}, 'events': {}, 'card': None, 'last_sequence': 0}
            fresh = True
        yield path, state, fresh
    finally:
        os.close(fd)


def deliver(value, allow_threads, *, token_loader, connection=None, reconcile_message=None, ledger_dir=None,
            recover_journal=False):
    """Durable server fencing. Fixture connection is the sole HTTP injection seam."""
    try:
        validate(value, allow_threads)
    except (ValueError, TypeError):
        return failure(value, 'rejected', 'invalid_payload')
    if 'operation' not in value:
        if recover_journal:
            return {'status': 'rejected'}
        return _deliver(value, allow_threads, token_loader=token_loader, connection=connection,
                        reconcile_message=reconcile_message)
    try:
        validate(value, allow_threads)
        if ledger_dir is None:
            raise ValueError('v2_server_journal_required')
        with _journal(ledger_dir, value['run_id']) as (path, state, fresh):
            bound = 'sender_profile' in value
            binding = dict(run_id=value['run_id'], thread_id=value['thread_id'], **profile_fields(value))
            # Missing binding is a historical DEFAULT record, never a new profile.
            # Only a brand-new empty journal can acquire a new explicit binding.
            if bound:
                if 'binding' not in state:
                    if not fresh:
                        return failure(value, 'rejected', 'journal_mismatch')
                    state['binding'] = binding
                    state['bot_id'] = None
                if state['binding'] != binding:
                    return failure(value, 'rejected', 'journal_mismatch')
                pinned = state.get('bot_id')
                if (pinned is not None and (not identifier(pinned)
                        or value['expected_bot_id'] is not None and pinned != value['expected_bot_id'])):
                    return failure(value, 'rejected', 'identity_mismatch')
            elif 'binding' in state or 'bot_id' in state:
                return failure(value, 'rejected', 'journal_mismatch')
            channel = state.setdefault('thread_id', value['thread_id'])
            if channel != value['thread_id']:
                raise ValueError('journal_channel_binding')
            key = str(value['sequence'])
            digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
            previous = state['records'].get(key)
            if previous and previous['digest'] != digest:
                raise ValueError('sequence_content_changed')
            if recover_journal:
                # Recovery is GET-only, even if the first request never arrived.
                # An absent authenticated response binding cannot justify a POST.
                if reconcile_message is not None:
                    raise ValueError('conflicting_recovery')
                reconcile_message = (previous['result'].get('message_id') or previous.get('known_message')) if previous else None
                if not reconcile_message:
                    return failure(value, 'uncertain', 'recovery_unbound')
            if previous and previous['result']['status'] == 'verified':
                known = validate_receipt(previous['result'], value)
                if bound and known['bot_id'] != state['bot_id']:
                    return failure(value, 'rejected', 'identity_mismatch')
                if reconcile_message is None:
                    return known
                if reconcile_message != known['message_id']:
                    raise ValueError('reconcile_identity')
                return _deliver(value, allow_threads, token_loader=token_loader, connection=connection,
                                reconcile_message=reconcile_message, response_bound=True,
                                pinned_bot_id=state.get('bot_id'))
            if previous and previous['result']['status'] == 'uncertain' and reconcile_message is None:
                return failure(value, 'uncertain', 'recovery_unbound')
            if value['sequence'] <= state['last_sequence']:
                raise ValueError('obsolete_sequence')
            if any(r['result']['status'] == 'uncertain' for k, r in state['records'].items() if k != key):
                raise ValueError('earlier_write_uncertain')
            if value['operation'] == 'CARD_PATCH' and value['card_receipt'] != state['card']:
                raise ValueError('unregistered_card')
            if value['operation'] == 'CARD_CREATE' and state['card'] is not None:
                raise ValueError('card_already_created')
            if value['event_id'] in state['events']:
                raise ValueError('event_already_reported')
            known_message = previous.get('known_message') if previous else None
            if reconcile_message and known_message and reconcile_message != known_message:
                raise ValueError('reconcile_identity')
            state['records'][key] = {'digest': digest, 'result': {'status': 'uncertain'}, 'known_message': known_message}
            _save(path, state)
            def observed_response(message_id):
                state['records'][key]['known_message'] = message_id
                _save(path, state)
            def observed_identity(bot_id):
                if state['bot_id'] not in (None, bot_id):
                    raise ValueError('identity_mismatch')
                state['bot_id'] = bot_id
                _save(path, state)
            result = _deliver(value, allow_threads, token_loader=token_loader, connection=connection,
                              reconcile_message=reconcile_message, on_response=observed_response,
                              response_bound=bool(known_message and known_message == reconcile_message),
                              on_identity=observed_identity if bound else None,
                              pinned_bot_id=state.get('bot_id'))
            if reconcile_message and result['status'] == 'rejected':
                result = dict(result, status='uncertain')
            state['records'][key]['result'] = result
            if result['status'] == 'verified':
                state['events'][value['event_id']] = result
                state['last_sequence'] = value['sequence']
                if value['operation'] == 'CARD_CREATE':
                    state['card'] = result
            _save(path, state)
            return result
    except Exception:
        return failure(value, 'uncertain', 'journal_mismatch')


def _default_profile_token(runtime_root):
    """SERVER ONLY. Never called by tests/help/dry-run or by the Mac bridge."""
    # Pin the existing named loader to the default profile even in an inherited
    # nondefault server shell. Do not inspect any other profile or credential.
    default_home = Path.home() / '.hermes'
    if (os.environ.get('HERMES_PROFILE') not in (None, '', 'default')
            or os.environ.get('HERMES_HOME') not in (None, '', str(default_home))):
        raise ValueError('default_profile_required')
    os.environ['HERMES_HOME'] = str(default_home)
    os.environ.pop('HERMES_PROFILE', None)
    sys.path.insert(0, str(runtime_root))
    with open(os.devnull, 'w') as sink, redirect_stdout(sink), redirect_stderr(sink):
        from hermes_cli.send_cmd import _load_hermes_env
        _load_hermes_env()
    return os.environ.get('DISCORD_BOT_TOKEN')


def _profile_token(runtime_root, sender_profile, *, canonical_loader=None, key_names=None):
    """SERVER ONLY opaque loader. Test seams must supply BOTH fake functions.

    Never receives a home/env key from a payload. The canonical runtime owns
    parsing; only its key-name metadata is used to exclude fallback credentials.
    """
    validate_profile(sender_profile, '1')
    if sender_profile is None:
        raise ValueError('profile_mismatch')
    home = Path.home() / '.hermes'
    if sender_profile == 'koharu':
        home = home / 'profiles' / 'koharu'
    if home.resolve() != home:
        raise ValueError('profile_mismatch')
    for name in ('.env', '.op.env', 'config.yaml'):
        if (home / name).resolve() != home / name:
            raise ValueError('profile_mismatch')
    original = dict(os.environ)
    loader_module = None
    saved_hooks = {}
    try:
        # dotenv expansion must not consume a credential inherited from another
        # profile. Keep only interpreter/OS location and locale variables.
        os.environ.clear()
        for key in ('HOME', 'PATH', 'USER', 'LANG', 'LC_ALL', 'TZ', 'TMPDIR',
                    'SYSTEMROOT', 'USERPROFILE', 'SSL_CERT_FILE'):
            if key in original:
                os.environ[key] = original[key]
        os.environ['HERMES_HOME'] = str(home)
        os.environ['HERMES_PROFILE'] = sender_profile
        with open(os.devnull, 'w') as sink, redirect_stdout(sink), redirect_stderr(sink):
            if canonical_loader is None and key_names is None:
                sys.path.insert(0, str(runtime_root))
                from hermes_cli.env_loader import load_hermes_dotenv, _env_keys_defined_in_dotenv
                from hermes_cli.managed_scope import get_managed_dir
                managed = get_managed_dir()
                if managed is not None and (managed / '.env').exists():
                    # The canonical loader applies a machine-wide env last. It
                    # cannot prove selected-home provenance in that topology.
                    # Refuse before reading that file or any credential.
                    raise ValueError('profile_mismatch')
                canonical_loader, key_names = load_hermes_dotenv, _env_keys_defined_in_dotenv
                loader_module = sys.modules['hermes_cli.env_loader']
            if canonical_loader is None or key_names is None:
                raise ValueError('loader_fixture_pair_required')
            selected_keys = key_names(home / '.env')
            bootstrap_keys = key_names(home / '.op.env')
            if (selected_keys | bootstrap_keys) & {'HERMES_MANAGED_DIR', 'HERMES_HOME', 'HERMES_PROFILE'}:
                raise ValueError('profile_mismatch')
            if loader_module is not None:
                # This short-lived helper needs canonical credential parsing,
                # not file repairs or terminal config's home-creation side effects.
                for name in ('_sanitize_env_file_if_needed', '_reapply_terminal_config_bridge', '_apply_managed_env'):
                    saved_hooks[name] = getattr(loader_module, name)
                    setattr(loader_module, name, lambda *args, **kwargs: None)
            canonical_loader(hermes_home=home, project_env=None, load_external_secrets=False)
            # Canonical dotenv syntax is richer than the key-name scanner. Even
            # quoted/export/BOM selectors cannot activate an overlay (hook above)
            # or alter the selected identity without failing closed here.
            if (os.environ.get('HERMES_MANAGED_DIR') is not None
                    or os.environ.get('HERMES_HOME') != str(home)
                    or os.environ.get('HERMES_PROFILE') != sender_profile):
                raise ValueError('profile_mismatch')
            # A missing selected-profile assignment cannot inherit a managed,
            # project, shell, or other profile's Discord token.
            if 'DISCORD_BOT_TOKEN' not in selected_keys:
                return None
            return os.environ.get('DISCORD_BOT_TOKEN')
    finally:
        for name, hook in saved_hooks.items():
            setattr(loader_module, name, hook)
        os.environ.clear()
        os.environ.update(original)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', required=True)
    parser.add_argument('--allow-thread', action='append', required=True)
    parser.add_argument('--reconcile-message', help='GET only: exact previously posted message ID')
    parser.add_argument('--recover-journal', action='store_true', help='GET only using an authenticated message ID already in the journal')
    parser.add_argument('--delivery-state-dir', help='Private durable server journal; required for v2 sends')
    parser.add_argument('--sender-profile', choices=('default', 'koharu'))
    parser.add_argument('--expected-bot-id')
    parser.add_argument('--preflight', action='store_true', help='Identity and target GET only; no journal/message writes')
    parser.add_argument('--dry-run', action='store_true', help='Validate stdin without loading credentials or networking')
    args = parser.parse_args(argv)
    value = dict(sender_profile=args.sender_profile) if args.sender_profile is not None else {}
    previous_handler = signal.getsignal(signal.SIGALRM)
    dispatched = False
    try:
        validate_profile(args.sender_profile, args.expected_bot_id)
        if args.preflight and (args.sender_profile is None or args.reconcile_message or args.recover_journal):
            raise ValueError('profile_mismatch')
        if not Path(args.runtime_root).is_absolute() or not all(identifier(t) for t in args.allow_thread):
            raise ValueError('operator_arguments')
        if args.reconcile_message is not None and not identifier(args.reconcile_message):
            raise ValueError('message_identity')
        def expired(*_):
            raise TimeoutError('deadline')
        signal.signal(signal.SIGALRM, expired)
        signal.alarm(30)  # Includes stdin and both HTTP responses.
        value = validate(decode(sys.stdin.buffer.read(MAX_INPUT + 1)), set(args.allow_thread))
        if (value.get('sender_profile') != args.sender_profile
                or value.get('expected_bot_id') != args.expected_bot_id):
            result = failure(value if 'sender_profile' in value else {'sender_profile': args.sender_profile},
                             'rejected', 'profile_mismatch')
        elif args.dry_run:
            result = {'status': 'validated', 'run_id': value['run_id'], 'sequence': value['sequence']}
        else:
            dispatched = True
            loader = (lambda: _default_profile_token(args.runtime_root)) if args.sender_profile is None else (
                lambda: _profile_token(args.runtime_root, args.sender_profile))
            if args.preflight:
                result = _deliver(value, set(args.allow_thread), token_loader=loader, preflight=True)
            else:
                result = deliver(value, set(args.allow_thread), token_loader=loader,
                                 reconcile_message=args.reconcile_message, ledger_dir=args.delivery_state_dir,
                                 recover_journal=args.recover_journal)
    except Exception:
        result = failure(value, 'uncertain' if dispatched else 'rejected',
                         'transport_error' if dispatched else 'invalid_payload')
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)
    print(json.dumps(result), flush=True)
    return 0 if result['status'] in ('verified', 'validated', 'ready') else 75


if __name__ == '__main__':
    raise SystemExit(main())
