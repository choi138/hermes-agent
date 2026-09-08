#!/usr/bin/env python3
"""Opt-in server helper. Stages as ONE stdlib-only file beside a Hermes runtime.

Only main's non-dry-run path loads the default profile credential. No import
side effects, redirects, attachments, channel lookup or arbitrary endpoint.
"""
import argparse
from contextlib import redirect_stdout, redirect_stderr
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
    if not isinstance(value, dict) or set(value) != {'run_id', 'sequence', 'thread_id', 'content', 'content_digest'}:
        raise ValueError('payload_schema')
    if (not isinstance(value['run_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value['run_id'])
            or type(value['sequence']) is not int or not 1 <= value['sequence'] <= 1000000
            or not identifier(value['thread_id']) or value['thread_id'] not in allow_threads):
        raise ValueError('payload_identity')
    content = value['content']
    if (not isinstance(content, str) or not 1 <= len(content) <= 1200
            or not content.startswith('작업 진행 상황을 전해드려요.\n')
            or not re.fullmatch(r'[가-힣A-Za-z0-9\n •·.,:/_\\()%\-]+', content)
            or re.search(r'MEDIA|https?\s*:|www\.|(?:sk|ghp|github_pat|xox[baprs])[-_]|[A-Za-z0-9]{24,}', content, re.I)
            or re.search(r'\\(?!_)', content)
            or value['content_digest'] != hashlib.sha256(content.encode()).hexdigest()):
        raise ValueError('unsafe_content')
    return value


def receipt(value, message_id):
    return {'status': 'verified', 'run_id': value['run_id'], 'sequence': value['sequence'],
            'thread_id': value['thread_id'], 'content_digest': value['content_digest'], 'message_id': message_id}


def validate_receipt(result, value):
    if (not isinstance(result, dict) or not identifier(result.get('message_id'))
            or type(result.get('sequence')) is not int):
        raise ValueError('receipt_schema')
    if result != receipt(value, result['message_id']):
        raise ValueError('receipt_identity')
    return result


def deliver(value, allow_threads, *, token_loader, connection=None, reconcile_message=None):
    """HTTP connection injection is fixture-only; production always uses discord.com."""
    try:
        validate(value, allow_threads)
        if reconcile_message is not None and not identifier(reconcile_message):
            raise ValueError('message_identity')
    except (ValueError, TypeError):
        return {'status': 'rejected'}
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

        message_id = reconcile_message
        if message_id is None:
            attempted = True  # Errors from request onward may follow an accepted POST.
            conn.request('POST', base, body=json.dumps({'content': value['content'],
                         'allowed_mentions': MENTIONS}, ensure_ascii=True).encode(), headers=headers)
            status, posted = read()
            if status in (400, 401, 403, 404, 405, 413, 429):
                return {'status': 'rejected'}
            if status not in (200, 201) or not isinstance(posted, dict):
                return {'status': 'uncertain'}
            message_id = posted.get('id')
            if (not identifier(message_id) or posted.get('channel_id') != value['thread_id']
                    or posted.get('content') != value['content']):
                return {'status': 'uncertain'}
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
                             reconcile_message=args.reconcile_message)
        signal.alarm(0)
    except Exception:
        result = {'status': 'uncertain'}
    print(json.dumps(result), flush=True)
    return 0 if result['status'] in ('verified', 'validated') else 75


if __name__ == '__main__':
    raise SystemExit(main())
