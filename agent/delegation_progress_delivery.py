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
from scripts.delegation_progress_discord_send import decode, identifier, validate, validate_receipt


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
            if code == 75 and decode(bytes(output)) not in ({'status': 'rejected'}, {'status': 'uncertain'}):
                raise ValueError('nonzero_success_receipt')
        return bytes(output)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


class SSHSender:
    def __init__(self, host, remote_python, runtime_root, helper, allow_threads, *, timeout=40, transport=ssh_transport):
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
        self.allow_threads, self.timeout, self.transport = tuple(allow_threads), timeout, transport

    def argv(self, reconcile_message=None):
        remote = [self.remote_python, self.helper, '--runtime-root', self.runtime_root]
        for thread in self.allow_threads:
            remote.extend(['--allow-thread', thread])
        if reconcile_message is not None:
            if not identifier(reconcile_message):
                raise ValueError('message_identity')
            remote.extend(['--reconcile-message', reconcile_message])
        return ['/usr/bin/ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                '-o', 'StrictHostKeyChecking=yes', '--', self.host, shlex.join(remote)]

    def send(self, value, *, reconcile_message=None):
        validate(value, self.allow_threads)
        try:
            raw = self.transport(self.argv(reconcile_message), json.dumps(value, ensure_ascii=True).encode(), self.timeout)
            result = decode(raw)
            if result in ({'status': 'rejected'}, {'status': 'uncertain'}):
                return result
            return validate_receipt(result, value)
        except Exception:
            return {'status': 'uncertain'}


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

    def drain_one(self, *, reconcile_message=None):
        # Also fences Python consumers that aren't inside the lifetime watcher.
        with _lock(self.progress.directory / 'delivery.lock'):
            message = self.progress.peek()
            if message is None:
                return {'status': 'empty'}
            manifest = self.progress.manifest
            if (message['run_id'] != manifest.run_id or message['thread_id'] != manifest.thread_id
                    or message['id'] != f"{manifest.run_id}:{message['sequence']}"):
                raise ValueError('outbox_identity')
            value = {k: message[k] for k in ('run_id', 'sequence', 'thread_id', 'content')}
            value['content_digest'] = hashlib.sha256(value['content'].encode()).hexdigest()
            validate(value, {manifest.thread_id})
            state = self._load()
            key = str(value['sequence'])
            record = state['records'].get(key)
            if key in state['records'] and (not isinstance(record, dict) or record.get('status') not in ('verified', 'uncertain', 'rejected')):
                raise ValueError('delivery_state')
            if record and record.get('status') == 'verified':
                validate_receipt(record, value)
            else:
                if record and record.get('status') == 'uncertain' and reconcile_message is None:
                    return {'status': 'uncertain'}
                if record and record.get('status') not in ('uncertain', 'rejected'):
                    raise ValueError('delivery_state')
                # Crash from here to receipt fsync leaves explicit uncertainty.
                state['records'][key] = {'status': 'uncertain'}
                _atomic(self.path, state)
                try:
                    result = self.sender.send(value, reconcile_message=reconcile_message)
                    if result not in ({'status': 'rejected'}, {'status': 'uncertain'}):
                        validate_receipt(result, value)
                        if any(r.get('message_id') == result['message_id'] for k, r in state['records'].items() if k != key):
                            raise ValueError('duplicate_discord_message')
                except Exception:
                    result = {'status': 'uncertain'}
                if reconcile_message is not None and result == {'status': 'rejected'}:
                    # GET rejection says nothing about whether the earlier POST
                    # succeeded. Never turn failed reconciliation into POST retry.
                    result = {'status': 'uncertain'}
                state['records'][key] = result
                _atomic(self.path, state)
                if result['status'] != 'verified':
                    return result
                record = result
            self.progress.ack(message['id'])
            return record
