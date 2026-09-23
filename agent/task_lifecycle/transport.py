"""Authenticated SSH CLI transport; the configured host owns the Mac profile.

The gateway injects identity before this boundary. This is not an HTTP API or
model tool and accepts no model-provided host, shell command or environment.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
import subprocess

from .types import LifecycleError


@dataclass(frozen=True)
class SSHExecutor:
    host: str
    python: str
    script: str
    profile_home: str
    executable_path: str
    login_shell: str | None = None

    def __post_init__(self):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]*', self.host):
            raise LifecycleError('Configured SSH host must be an alias/hostname, not options')
        for value in (self.python, self.script, self.profile_home):
            if not Path(value).is_absolute() or '\0' in value:
                raise LifecycleError('Remote configuration requires absolute paths')
        if not self.executable_path or any(not Path(p).is_absolute() for p in self.executable_path.split(':')):
            raise LifecycleError('Remote PATH must contain only explicit absolute directories')
        if self.login_shell not in {None, '/bin/zsh'}:
            raise LifecycleError('Only the configured macOS zsh login environment is supported')

    def call(self, action, *arguments, payload=None):
        if action not in {'import-request', 'export-result', 'status', 'receipt', 'cancel', 'verify'}:
            raise LifecycleError('Unsupported remote lifecycle operation')
        argv = ['env', 'HERMES_HOME='+self.profile_home, 'PATH='+self.executable_path,
                self.python, self.script, 'lifecycle', action, *arguments]
        if self.login_shell:
            # The user's existing startup files may configure CLI credentials.
            # No credential copying, parsing or forwarding over command args.
            argv = [self.login_shell, '-lic', 'exec '+shlex.join(argv)]
        completed = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                    self.host, shlex.join(argv)],
            input=json.dumps(payload,ensure_ascii=False) if payload is not None else None,
            capture_output=True, text=True, timeout=45)
        if completed.returncode != 0:
            # Lost response is ambiguous. Caller reconciles by original key;
            # the same envelope is safe to resubmit, never a new revision.
            raise LifecycleError('SSH operation failed or lost its response; retain the original request key')
        try:
            return json.loads(completed.stdout)
        except ValueError as exc:
            raise LifecycleError('SSH response was not a lifecycle receipt') from exc

    def submit(self, envelope):
        return self.call('import-request', payload=envelope)

    def receive(self, run_id, *, envelope, content, attachments=()):
        from .remote_result import receive_result
        package = self.call('export-result', run_id, payload={'content':content,'attachments':list(attachments)})
        return receive_result(package,envelope=envelope,expected_run_id=run_id,
                              content=content,attachments=attachments)
