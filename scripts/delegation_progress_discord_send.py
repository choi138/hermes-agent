#!/usr/bin/env python3
"""Opt-in server helper. Stages as ONE stdlib-only file beside a Hermes runtime.

Only main's non-dry-run path loads the default profile credential. No import
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
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError('payload_schema')
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


def receipt(value, message_id):
    result = {'status': 'verified', 'run_id': value['run_id'], 'sequence': value['sequence'],
            'thread_id': value['thread_id'], 'content_digest': value['content_digest'], 'message_id': message_id}
    if 'operation' in value:
        result.update(operation=value['operation'], event_id=value['event_id'])
    return result


def validate_receipt(result, value):
    if (not isinstance(result, dict) or not identifier(result.get('message_id'))
            or type(result.get('sequence')) is not int):
        raise ValueError('receipt_schema')
    if result != receipt(value, result['message_id']):
        raise ValueError('receipt_identity')
    if value.get('operation') == 'CARD_PATCH' and result['message_id'] != value['card_receipt']['message_id']:
        raise ValueError('patch_target_identity')
    return result


def discord_body(value):
    body = {'content': value['content'], 'allowed_mentions': MENTIONS}
    if 'operation' in value and value['operation'] != 'CARD_PATCH':
        nonce = hashlib.sha256((value['run_id'] + ':' + str(value['sequence'])).encode()).hexdigest()[:24]
        body.update(nonce=nonce, enforce_nonce=True)
    return body


def _deliver(value, allow_threads, *, token_loader, connection=None, reconcile_message=None, on_response=None, response_bound=False):
    """HTTP connection injection is fixture-only; production always uses discord.com."""
    try:
        validate(value, allow_threads)
        if reconcile_message is not None and not identifier(reconcile_message):
            raise ValueError('message_identity')
    except (ValueError, TypeError):
        return {'status': 'rejected'}
    # A GET cannot establish which run sent identical content. Only the durable
    # authenticated sender response (or its verified receipt) binds recovery.
    if reconcile_message is not None and not response_bound:
        return {'status': 'uncertain'}
    attempted = False
    try:
        token = token_loader()
        if not isinstance(token, str) or not token or '\n' in token or '\r' in token:
            return {'status': 'rejected'}
        headers = {'Authorization': 'Bot ' + token, 'Content-Type': 'application/json',
                   'User-Agent': 'HermesProgressBridge/1.0'}
        conn = connection or http.client.HTTPSConnection('discord.com', timeout=10)
        base = '/api/v10/channels/' + value['thread_id'] + '/messages'

        def read():
            response = conn.getresponse()
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError('response_limit')
            return response.status, json.loads(raw)

        nonce = hashlib.sha256((value['run_id'] + ':' + str(value['sequence'])).encode()).hexdigest()[:24]
        message_id = reconcile_message
        target = value.get('card_receipt', {}).get('message_id') if value.get('card_receipt') else None
        if target and message_id and message_id != target:
            return {'status': 'rejected'}
        if message_id is None:
            attempted = True  # Errors from request onward may follow an accepted POST.
            body = discord_body(value)
            conn.request('PATCH' if target else 'POST', base + '/' + target if target else base,
                         body=json.dumps(body, ensure_ascii=True).encode(), headers=headers)
            status, posted = read()
            if status in (400, 401, 403, 404, 405, 413, 429):
                return {'status': 'rejected'}
            if status not in (200, 201) or not isinstance(posted, dict):
                return {'status': 'uncertain'}
            message_id = posted.get('id')
            if (not identifier(message_id) or target and message_id != target or posted.get('channel_id') != value['thread_id']
                    or posted.get('content') != value['content']
                    or 'operation' in value and not target and 'nonce' in posted and posted['nonce'] != nonce):
                return {'status': 'uncertain'}
            if on_response:
                on_response(message_id)
        attempted = True
        conn.request('GET', base + '/' + message_id, headers=headers)
        status, actual = read()
        if (status != 200 or not isinstance(actual, dict) or actual.get('id') != message_id
                or actual.get('channel_id') != value['thread_id'] or actual.get('content') != value['content']):
            return {'status': 'uncertain'}
        return receipt(value, message_id)
    except Exception:
        return {'status': 'uncertain' if attempted else 'rejected'}
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
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16 * 1024 * 1024
                        or info.st_uid != os.getuid() or info.st_mode & 0o077):
                    raise ValueError('journal_file')
                state = json.load(stream)
        except FileNotFoundError:
            state = {'records': {}, 'events': {}, 'card': None, 'last_sequence': 0}
        yield path, state
    finally:
        os.close(fd)


def deliver(value, allow_threads, *, token_loader, connection=None, reconcile_message=None, ledger_dir=None,
            recover_journal=False):
    """Durable server fencing. Fixture connection is the sole HTTP injection seam."""
    try:
        validate(value, allow_threads)
    except (ValueError, TypeError):
        return {'status': 'rejected'}
    if 'operation' not in value:
        if recover_journal:
            return {'status': 'rejected'}
        return _deliver(value, allow_threads, token_loader=token_loader, connection=connection,
                        reconcile_message=reconcile_message)
    try:
        validate(value, allow_threads)
        if ledger_dir is None:
            raise ValueError('v2_server_journal_required')
        with _journal(ledger_dir, value['run_id']) as (path, state):
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
                    return {'status': 'uncertain'}
            if previous and previous['result']['status'] == 'verified':
                known = validate_receipt(previous['result'], value)
                if reconcile_message is None:
                    return known
                if reconcile_message != known['message_id']:
                    raise ValueError('reconcile_identity')
                return _deliver(value, allow_threads, token_loader=token_loader, connection=connection,
                                reconcile_message=reconcile_message, response_bound=True)
            if previous and previous['result']['status'] == 'uncertain' and reconcile_message is None:
                return {'status': 'uncertain'}
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
            result = _deliver(value, allow_threads, token_loader=token_loader, connection=connection,
                              reconcile_message=reconcile_message, on_response=observed_response,
                              response_bound=bool(known_message and known_message == reconcile_message))
            if reconcile_message and result['status'] == 'rejected':
                result = {'status': 'uncertain'}
            state['records'][key]['result'] = result
            if result['status'] == 'verified':
                state['events'][value['event_id']] = result
                state['last_sequence'] = value['sequence']
                if value['operation'] == 'CARD_CREATE':
                    state['card'] = result
            _save(path, state)
            return result
    except Exception:
        return {'status': 'uncertain'}


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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', required=True)
    parser.add_argument('--allow-thread', action='append', required=True)
    parser.add_argument('--reconcile-message', help='GET only: exact previously posted message ID')
    parser.add_argument('--recover-journal', action='store_true', help='GET only using an authenticated message ID already in the journal')
    parser.add_argument('--delivery-state-dir', help='Private durable server journal; required for v2 sends')
    parser.add_argument('--dry-run', action='store_true', help='Validate stdin without loading credentials or networking')
    args = parser.parse_args(argv)
    try:
        if not Path(args.runtime_root).is_absolute() or not all(identifier(t) for t in args.allow_thread):
            raise ValueError('operator_arguments')
        if args.reconcile_message is not None and not identifier(args.reconcile_message):
            raise ValueError('message_identity')
        def expired(*_):
            raise TimeoutError('deadline')
        signal.signal(signal.SIGALRM, expired)
        signal.alarm(30)  # Includes stdin and both HTTP responses.
        value = validate(decode(sys.stdin.buffer.read(MAX_INPUT + 1)), set(args.allow_thread))
        if args.dry_run:
            result = {'status': 'validated', 'run_id': value['run_id'], 'sequence': value['sequence']}
        else:
            result = deliver(value, set(args.allow_thread), token_loader=lambda: _default_profile_token(args.runtime_root),
                             reconcile_message=args.reconcile_message, ledger_dir=args.delivery_state_dir,
                             recover_journal=args.recover_journal)
        signal.alarm(0)
    except Exception:
        result = {'status': 'uncertain'}
    print(json.dumps(result), flush=True)
    return 0 if result['status'] in ('verified', 'validated') else 75


if __name__ == '__main__':
    raise SystemExit(main())
