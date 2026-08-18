import json

from tools.runtime_control_tool import _MODEL_SWITCH_SCHEMA


def test_model_switch_schema_exposes_max_reasoning_effort():
    schema = json.loads(json.dumps(_MODEL_SWITCH_SCHEMA))
    reasoning_efforts = schema["parameters"]["properties"]["reasoning_effort"]["enum"]

    assert "max" in reasoning_efforts
