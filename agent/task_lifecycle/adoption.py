"""Read-only agentsx adoption proof for the actual executor context pack."""
import hashlib
from pathlib import Path
import subprocess

from .types import LifecycleError


def inspect_adoption(root, executable):
    root, executable = Path(root), Path(executable)
    if not executable.is_absolute() or not executable.is_file():
        raise LifecycleError('An installed agentsx executable is required')
    result = subprocess.run([str(executable), 'check', str(root)], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise LifecycleError('agentsx adoption check failed; install/sync explicitly before executing')
    paths = [root/'AGENTS.md', root/'.agents/.engine-lock.json', root/'.agents/verify.sh']
    paths += sorted((root/'.agents/hooks').glob('*.ts'))
    if any(p.is_symlink() or not p.is_file() for p in paths):
        raise LifecycleError('Incomplete agentsx adoption files')
    files = {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    return dict(executable=str(executable), files=files, check_output=result.stdout,
                # This proves policy input, not that native CLI hooks executed.
                native_hooks_verified=False, policy_text=(root/'AGENTS.md').read_text())
