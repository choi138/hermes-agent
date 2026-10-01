"""Opt-in Mac outbox consumer. Local durable receipts precede collector ack."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import shlex
import subprocess
import time

from agent.delegation_progress import _atomic, _lock, _read
from scripts.delegation_progress_discord_send import (decode, identifier, validate, validate_receipt,
    validate_profile, profile_fields, validate_failure, failure)


def ssh_transport(argv, data, timeout, *, popen=subprocess.Popen):
    """Bounded live pipes; the CLI never exposes the fixture popen seam."""
    process = popen(argv, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + timeout
    output, position, total = bytearray(), 0, 0
    try:
        with selectors.DefaultSelector() as selector:
            for stream, mode in ((process.stdin, selectors.EVENT_WRITE), (process.stdout, selectors.EVENT_READ),
                                 (process.stderr, selectors.EVENT_READ)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, mode)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise TimeoutError('sender_timeout')
                for key, _ in selector.select(.05):
                    if key.fileobj is process.stdin:
                        position += os.write(key.fd, data[position:position + 4096])
                        if position == len(data):
                            selector.unregister(key.fileobj)
                            process.stdin.close()
                    else:
                        chunk = os.read(key.fd, 4096)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > 16384:
                            raise ValueError('sender_output_limit')
                        if key.fileobj is process.stdout:
                            output.extend(chunk)
            code = process.wait(timeout=max(.001, deadline - time.monotonic()))
            # 75 is the helper's explicit rejected/uncertain response; all other
            # SSH exit codes are ambiguous, irrespective of stdout.
            if code not in (0, 75):
                raise ValueError('transport_failed')
            if code == 75:
                validate_failure(decode(bytes(output)))
        return bytes(output)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


class SSHSender:
    def __init__(self, host, remote_python, runtime_root, helper, allow_threads, *, timeout=40, transport=ssh_transport,
                 server_state_dir=None, sender_profile=None, expected_bot_id=None):
        validate_profile(sender_profile, expected_bot_id)
        self.sender_profile, self.expected_bot_id = sender_profile, expected_bot_id
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,200}', host):
            raise ValueError('ssh_host')
        for path in (remote_python, runtime_root, helper):
            if not isinstance(path, str) or not Path(path).is_absolute() or len(path) > 1024 or any(ord(c) < 32 for c in path):
                raise ValueError('remote_path')
        if not allow_threads or not all(identifier(t) for t in allow_threads):
            raise ValueError('allow_threads')
        if isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError('sender_timeout')
        self.host, self.remote_python, self.runtime_root, self.helper = host, remote_python, runtime_root, helper
        if server_state_dir is not None and (not Path(server_state_dir).is_absolute() or any(ord(c) < 32 for c in server_state_dir)):
            raise ValueError('server_state_dir')
        self.server_state_dir = server_state_dir
        self.allow_threads, self.timeout, self.transport = tuple(allow_threads), timeout, transport

    def argv(self, reconcile_message=None, *, recover_journal=False, preflight=False):
        remote = [self.remote_python, self.helper, '--runtime-root', self.runtime_root]
        if self.server_state_dir:
            remote.extend(['--delivery-state-dir', self.server_state_dir])
        for thread in self.allow_threads:
            remote.extend(['--allow-thread', thread])
        if reconcile_message is not None:
            if not identifier(reconcile_message):
                raise ValueError('message_identity')
            remote.extend(['--reconcile-message', reconcile_message])
        if recover_journal:
            remote.append('--recover-journal')
        if preflight:
            if self.sender_profile is None or reconcile_message or recover_journal:
                raise ValueError('profile_mismatch')
            remote.append('--preflight')
        if self.sender_profile is not None:
            remote.extend(['--sender-profile', self.sender_profile])
            if self.expected_bot_id is not None:
                remote.extend(['--expected-bot-id', self.expected_bot_id])
        return ['/usr/bin/ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                '-o', 'StrictHostKeyChecking=yes', '--', self.host, shlex.join(remote)]

    def send(self, value, *, reconcile_message=None, recover_journal=False, preflight=False):
        validate(value, self.allow_threads)
        if (value.get('sender_profile') != self.sender_profile or value.get('expected_bot_id') != self.expected_bot_id):
            return failure(value, 'rejected', 'profile_mismatch')
        try:
            raw = self.transport(self.argv(reconcile_message, recover_journal=recover_journal, preflight=preflight),
                                 json.dumps(value, ensure_ascii=True).encode(), self.timeout)
            result = decode(raw)
            if isinstance(result, dict) and result.get('status') in ('rejected', 'uncertain'):
                return validate_failure(result)
            if preflight:
                expected = dict(status='ready', run_id=value['run_id'], thread_id=value['thread_id'],
                                **profile_fields(value), bot_id=result.get('bot_id'))
                if (result != expected or not identifier(result.get('bot_id'))
                        or self.expected_bot_id is not None and result['bot_id'] != self.expected_bot_id):
                    raise ValueError('preflight_identity')
                return result
            return validate_receipt(result, value)
        except Exception:
            return failure(value, 'uncertain', 'transport_error')


class Delivery:
    def __init__(self, progress, sender):
        self.progress, self.sender = progress, sender
        self.path = progress.directory / 'delivery.json'

    def _load(self):
        try:
            state = json.loads(_read(self.progress.directory, self.path.name, 16 * 1024 * 1024))
        except FileNotFoundError:
            return {'binding': self.progress.manifest.binding(), 'records': {}}
        if (not isinstance(state, dict) or state.get('binding') != self.progress.manifest.binding()
                or not isinstance(state.get('records'), dict)):
            raise ValueError('delivery_identity')
        return state

    def claim_notice(self, *, dry_run=False):
        """Fence the automatic path BEFORE a coordinator sends this exact event."""
        from contextlib import nullcontext
        with nullcontext() if dry_run else _lock(self.progress.directory / 'delivery.lock'):
            message = self.progress.peek()
            if not message or message.get('operation') != 'NOTICE':
                raise ValueError('claim_requires_notice_head_drain_card_first')
            manifest = self.progress.manifest
            if message['run_id'] != manifest.run_id or message['thread_id'] != manifest.thread_id:
                raise ValueError('outbox_identity')
            if profile_fields(message) != manifest.sender_identity():
                raise ValueError('outbox_identity')
            value = {k: message[k] for k in ('run_id', 'sequence', 'thread_id', 'content', 'operation', 'event_id')}
            value.update(content_digest=hashlib.sha256(value['content'].encode()).hexdigest(), card_receipt=None)
            value.update(manifest.sender_identity())
            validate(value, {manifest.thread_id})
            state = self._load()
            key = str(message['sequence'])
            if key in state['records']:
                raise ValueError('already_attempted_use_get_only_reconciliation')
            if not dry_run:
                state['records'][key] = {'status': 'uncertain'}
                _atomic(self.path, state)
            # This payload must go through the same server helper and journal.
            # Arbitrary Discord POSTs cannot supply a durable run/response binding.
            return {'event_id': message['event_id'], 'payload': value}

    def drain_one(self, *, reconcile_message=None, reported_message=None, recover_journal=False):
        # Also fences Python consumers that aren't inside the lifetime watcher.
        with _lock(self.progress.directory / 'delivery.lock'):
            message = self.progress.peek()
            if message is None:
                return {'status': 'empty'}
            manifest = self.progress.manifest
            if (message['run_id'] != manifest.run_id or message['thread_id'] != manifest.thread_id
                    or message['id'] != f"{manifest.run_id}:{message['sequence']}"
                    or profile_fields(message) != manifest.sender_identity()):
                raise ValueError('outbox_identity')
            value = {k: message[k] for k in ('run_id', 'sequence', 'thread_id', 'content')}
            value['content_digest'] = hashlib.sha256(value['content'].encode()).hexdigest()
            value.update(manifest.sender_identity())
            state = self._load()
            if 'operation' in message:
                value.update(operation=message['operation'], event_id=message['event_id'], card_receipt=None)
                if message['operation'] == 'CARD_PATCH':
                    value['card_receipt'] = state.get('card')
                if reported_message is not None:
                    if message['operation'] != 'NOTICE' or reconcile_message is not None:
                        raise ValueError('reported_message_requires_notice_head')
                    reconcile_message = reported_message
            elif reported_message is not None:
                raise ValueError('reported_message_requires_v2')
            validate(value, {manifest.thread_id})
            key = str(value['sequence'])
            record = state['records'].get(key)
            if key in state['records'] and (not isinstance(record, dict) or record.get('status') not in ('verified', 'uncertain', 'rejected')):
                raise ValueError('delivery_state')
            if record and record.get('status') == 'verified':
                validate_receipt(record, value)
            else:
                if record and record.get('status') == 'uncertain' and reconcile_message is None and not recover_journal:
                    return {'status': 'uncertain'}
                if record and record.get('status') not in ('uncertain', 'rejected'):
                    raise ValueError('delivery_state')
                # Crash from here to receipt fsync leaves explicit uncertainty.
                state['records'][key] = {'status': 'uncertain'}
                _atomic(self.path, state)
                try:
                    kwargs = {'reconcile_message': reconcile_message}
                    if recover_journal and record and record.get('status') == 'uncertain':
                        kwargs['recover_journal'] = True
                    result = self.sender.send(value, **kwargs)
                    if isinstance(result, dict) and result.get('status') in ('rejected', 'uncertain'):
                        validate_failure(result)
                    else:
                        validate_receipt(result, value)
                        if value.get('operation') != 'CARD_PATCH' and any(r.get('message_id') == result['message_id'] for k, r in state['records'].items() if k != key):
                            raise ValueError('duplicate_discord_message')
                except Exception:
                    result = failure(value, 'uncertain', 'transport_error')
                if (reconcile_message is not None or kwargs.get('recover_journal')) and result['status'] == 'rejected':
                    # GET rejection says nothing about whether the earlier POST
                    # succeeded. Never turn failed reconciliation into POST retry.
                    result = dict(result, status='uncertain')
                state['records'][key] = result
                if result['status'] == 'verified' and value.get('operation') == 'CARD_CREATE':
                    state['card'] = result
                _atomic(self.path, state)
                if result['status'] != 'verified':
                    return result
                record = result
            self.progress.ack(message['id'])
            return record
