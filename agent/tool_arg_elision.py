"""Non-replayable markers for compacted historical tool arguments.

Long tool-call string arguments are removed from active model context to save
tokens.  A marker must never preserve a literal prefix of the original value:
for mutating tools such as ``write_file`` that prefix can look like complete
input and be replayed later, turning a context preview into real file bytes.
"""

from __future__ import annotations

import hashlib
import re


TOOL_ARG_ELISION_PREFIX = "[HERMES_CONTEXT_ELIDED "
_TOOL_ARG_ELISION_RE = re.compile(
    r"^\[HERMES_CONTEXT_ELIDED chars=\d+ sha256=[0-9a-f]{12} "
    r"DO_NOT_REUSE_AS_INPUT\]$"
)


def make_tool_arg_elision(value: str) -> str:
    """Return a compact, valid-string tombstone with no original prefix."""

    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return (
        f"{TOOL_ARG_ELISION_PREFIX}chars={len(value)} sha256={digest} "
        "DO_NOT_REUSE_AS_INPUT]"
    )


def is_tool_arg_elision(value: str) -> bool:
    """Whether ``value`` is exactly a Hermes tool-argument tombstone."""

    return isinstance(value, str) and _TOOL_ARG_ELISION_RE.fullmatch(value.strip()) is not None
