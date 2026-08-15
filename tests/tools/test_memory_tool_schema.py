"""Schema-shape tests for the built-in memory tool.

The memory tool previously used ``allOf: [{if: ..., then: {required: ...}}]``
at the top level of ``parameters`` to hint per-action required fields.  That
form was:

  1. Ignored by every provider (Chat Completions doesn't honour ``if/then``
     on function schemas), so it never actually enforced anything.
  2. **Rejected outright by strict backends** — OpenAI's Codex endpoint
     (``chatgpt.com/backend-api/codex``, gpt-5.x) returns
     ``Invalid schema for function 'memory': schema must have type 'object'
     and not have 'oneOf'/'anyOf'/'allOf'/'enum'/'not' at the top level``.

We now rely on the runtime handler (``memory_tool()`` in ``tools/memory_tool.py``)
to validate required fields per action and return actionable error messages.
These tests guard the schema against regressing back to a shape strict
backends reject.
"""

import json
import inspect
from types import SimpleNamespace

from agent import agent_runtime_helpers, tool_executor
from tools.memory_tool import MEMORY_SCHEMA, MemoryStore


_FORBIDDEN_TOP_LEVEL_KEYS = ("allOf", "anyOf", "oneOf", "enum", "not")


def test_memory_schema_has_no_forbidden_top_level_combinators():
    """OpenAI's Codex backend rejects these at the top level of parameters."""
    params = MEMORY_SCHEMA["parameters"]
    for key in _FORBIDDEN_TOP_LEVEL_KEYS:
        assert key not in params, (
            f"top-level {key!r} in memory tool parameters will break the "
            "Codex backend (chatgpt.com/backend-api/codex). Per-action "
            "required-field checks belong in the runtime handler, not the schema."
        )


def test_memory_schema_is_json_serializable():
    json.dumps(MEMORY_SCHEMA)


def test_memory_invoke_tool_forwards_reason_to_runtime_handler(tmp_path, monkeypatch):
    """Agent-level memory dispatch must pass the schema's reason argument through.

    This catches half-patches where the tool schema/backend accepts ``reason``
    but agent intercept paths still call ``memory_tool()`` without forwarding it.
    """
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    store = MemoryStore(memory_char_limit=1000, user_char_limit=1000)
    store.load_from_disk()
    agent = SimpleNamespace(
        _memory_store=store,
        _memory_manager=None,
        session_id="test-session",
    )

    result = json.loads(
        agent_runtime_helpers.invoke_tool(
            agent,
            "memory",
            {
                "action": "add",
                "target": "memory",
                "content": "Runtime dispatch reason smoke",
                "reason": "This is a global environment invariant for dispatch regression testing.",
            },
            effective_task_id="test-task",
            pre_tool_block_checked=True,
        )
    )

    assert result["success"] is True
    assert "Runtime dispatch reason smoke" in store.memory_entries


def test_memory_intercept_paths_forward_reason_argument():
    assert 'reason=next_args.get("reason", "")' in inspect.getsource(agent_runtime_helpers.invoke_tool)
    assert 'reason=next_args.get("reason", "")' in inspect.getsource(tool_executor.execute_tool_calls_sequential)
