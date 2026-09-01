"""Contract tests for direct-agent request classification."""

from __future__ import annotations

import json

from agent.direct_agent_classification import (
    ClassificationResult,
    classification_json_schema,
    parse_classification_output,
)


def _valid_payload() -> dict:
    return {
        "schema_version": "1.0",
        "intent": {
            "kind": "code_change",
            "summary": "Add a strict classifier contract",
            "requested_outcome": "A validated classification object",
        },
        "risk": {
            "level": "medium",
            "side_effect_categories": ["filesystem_write"],
            "rationale": "The request asks for repository changes.",
        },
        "memory_query": {
            "required": False,
            "query": None,
            "reason": "The request is self-contained.",
        },
        "execution_target": {
            "lane": "coding",
            "host": "developer-workstation",
            "workdir": "/workspace/hermes-agent",
        },
        "uncertainties": ["The implementation module is not selected."],
    }


def test_valid_round_trip_and_json_schema_are_stable() -> None:
    raw = json.dumps(_valid_payload())

    result = parse_classification_output(raw)

    assert isinstance(result, ClassificationResult)
    assert result.model_dump(mode="json") == _valid_payload()
    schema = classification_json_schema()
    assert schema == classification_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "schema_version",
        "intent",
        "risk",
        "memory_query",
        "execution_target",
        "uncertainties",
    }
