"""Fail-open loader for operator-authored gateway mood prompts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


MAX_MOOD_BYTES = 8 * 1024


def load_mood_file(moods: Any, mood: str) -> Optional[str]:
    """Load ``<moods.dir>/<mood>.md`` without raising into a gateway turn.

    Files are read as UTF-8 and capped by bytes so an operator asset cannot
    grow the call-time system prompt without bound. Whitespace-only files are
    treated as empty, while meaningful file whitespace is otherwise preserved.
    """
    try:
        resolved_dir = getattr(moods, "resolved_dir", None)
        if callable(resolved_dir):
            directory = Path(resolved_dir()).expanduser()
        else:
            directory = Path(str(getattr(moods, "dir", "") or "")).expanduser()
        with (directory / f"{mood}.md").open("rb") as mood_file:
            raw = mood_file.read(MAX_MOOD_BYTES)
        content = raw.decode("utf-8", errors="ignore")
        return content if content.strip() else None
    except Exception:
        return None


__all__ = ["MAX_MOOD_BYTES", "load_mood_file"]
