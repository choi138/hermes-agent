"""Bounded script reads for terminal lifecycle checks."""
import shlex
from pathlib import Path
from typing import Any, Optional


def _read_script_for_guard(env: Any, guard_cwd: str, script_path: str, max_bytes: int) -> Optional[str]:
    """SSH reads bypass command sync; local reads retain lifecycle safety checks."""
    if env is None:
        return None
    from tools.environments.ssh import SSHEnvironment

    if isinstance(env, SSHEnvironment):
        return env.read_guard_script(script_path, cwd=guard_cwd, max_bytes=max_bytes)

    from cron.lifecycle_guard import _read_referenced_script

    local_path = Path(script_path).expanduser()
    if not local_path.is_absolute():
        local_path = Path(guard_cwd) / local_path
    text, unsafe = _read_referenced_script(local_path)
    if unsafe:
        raise ValueError("Refusing unsafe local guard input")
    if text is not None:
        return text
    try:
        result = env.execute(f"head -c {max_bytes + 1} < {shlex.quote(script_path)}")
        if result.get("returncode", -1) == 0:
            # The lifecycle scanner applies its shared binary/size sanitizer.
            return result.get("output", "")
    except Exception:
        pass
    return None
