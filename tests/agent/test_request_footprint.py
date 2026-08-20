from __future__ import annotations

import hashlib

from agent.request_footprint import (
    canonical_tool_schema_metrics,
    text_metrics,
)


def test_text_metrics_separates_char_and_utf8_byte_counts():
    value = "현재 단계"
    metrics = text_metrics(value)

    assert metrics.chars == len(value)
    assert metrics.utf8_bytes == len(value.encode("utf-8"))
    assert metrics.utf8_bytes > metrics.chars
    assert metrics.estimated_tokens == (len(value) + 3) // 4
    assert metrics.content_hash == hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_tool_metrics_hash_full_canonical_schema_content():
    first = [{"type": "function", "function": {"name": "x", "description": "a"}}]
    reordered = [{"function": {"description": "a", "name": "x"}, "type": "function"}]
    changed = [{"type": "function", "function": {"name": "x", "description": "b"}}]

    first_metrics = canonical_tool_schema_metrics(first)

    assert first_metrics == canonical_tool_schema_metrics(reordered)
    assert first_metrics.schema_hash != canonical_tool_schema_metrics(changed).schema_hash
    assert first_metrics.estimated_tokens == (first_metrics.json_bytes + 3) // 4
