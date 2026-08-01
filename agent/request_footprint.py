"""Deterministic, content-free request footprint measurements.

The gateway uses these metrics to enforce a schema-size budget, while the
conversation loop records the system/tool contribution to the first provider
request.  Only lengths and SHA-256 digests are returned; prompt contents are
never logged.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class TextMetrics:
    """Size and identity of an exact UTF-8 text payload."""

    chars: int
    utf8_bytes: int
    estimated_tokens: int
    content_hash: str


@dataclass(frozen=True)
class ToolSchemaMetrics:
    """Canonical metrics for an exact final tool-schema JSON array."""

    count: int
    json_bytes: int
    estimated_tokens: int
    schema_hash: str


def text_metrics(text: str) -> TextMetrics:
    """Measure an exact prompt string without retaining its contents."""

    value = str(text or "")
    encoded = value.encode("utf-8")
    return TextMetrics(
        chars=len(value),
        utf8_bytes=len(encoded),
        # Match the rough estimator used by context breakdown/compression.
        estimated_tokens=(len(value) + 3) // 4,
        content_hash=hashlib.sha256(encoded).hexdigest(),
    )


def canonical_tool_schema_metrics(
    definitions: Iterable[dict[str, Any]],
) -> ToolSchemaMetrics:
    """Measure/hash the exact canonical schema array sent by an agent."""

    schemas = list(definitions)
    canonical = json.dumps(
        schemas,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return ToolSchemaMetrics(
        count=len(schemas),
        json_bytes=len(canonical),
        estimated_tokens=(len(canonical) + 3) // 4,
        schema_hash=hashlib.sha256(canonical).hexdigest(),
    )
