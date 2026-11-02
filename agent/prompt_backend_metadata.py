"""One remote metadata round trip per project-context discovery, scoped to that read."""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath


def context_metadata(path: str, execute) -> dict[str, dict]:
    """Batch the fixed context candidates; file contents still follow normal precedence."""
    cwd = PurePosixPath(path)
    directories = [cwd, *cwd.parents]
    names = (".git", ".hermes.md", "HERMES.md", "AGENTS.override.md", "AGENTS.md", "agents.md")
    candidates = [str(directory / name) for directory in directories for name in names]
    candidates.extend(str(cwd / name) for name in (
        "CLAUDE.md", "CLAUDE.local.md", ".cursorrules", ".cursor/rules",
    ))
    # NUL-separated records keep spaces, shell metacharacters and newlines in filenames inert.
    command = (
        "for f in " + " ".join(shlex.quote(candidate) for candidate in candidates) + "; do "
        "flags=; for flag in e f d; do "
        'if test "-$flag" "$f"; then flags="$flags$flag"; fi; done; '
        "printf '%s\\0%s\\0' \"$f\" \"$flags\"; done"
    )
    result = execute(command)
    if result.get("returncode") != 0:
        raise OSError("Remote context metadata unavailable")
    fields = result.get("output", "").split("\0")
    if len(fields) != 2 * len(candidates) + 1 or fields[-1]:
        raise OSError("Incomplete remote context metadata")
    metadata = {}
    for remote_path, flags in zip(fields[0:-1:2], fields[1:-1:2], strict=True):
        for flag in ("e", "f", "d"):
            metadata[f"test -{flag} {shlex.quote(remote_path)}"] = {
                "returncode": 0 if flag in flags else 1, "output": "",
            }
    return metadata
