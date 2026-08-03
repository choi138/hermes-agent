"""Deterministic normalization for untrusted GitHub review text."""

from __future__ import annotations

import re

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SUGGESTION_RE = re.compile(
    r"```(?:suggestion|diff)[^\n]*\n.*?```",
    re.IGNORECASE | re.DOTALL,
)
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_review_text(value: object, *, limit: int) -> str:
    """Return bounded readable text without active mentions or review chrome."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 1:
        raise ValueError("limit must be greater than one")
    text = str(value or "")
    text = _HTML_COMMENT_RE.sub(" ", text)
    text = _SUGGESTION_RE.sub(" ", text)
    text = _IMAGE_RE.sub(" ", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = text.replace("@", "@\u200b")
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if len(text) <= limit:
        return text
    truncated = text[: limit - 1].rstrip()
    if " " in truncated:
        truncated = truncated.rsplit(" ", maxsplit=1)[0].rstrip()
    return truncated + "…"
