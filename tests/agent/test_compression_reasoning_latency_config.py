"""A compression-only effort override reaches the Codex wire without capping its summary."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.auxiliary_client import _CodexCompletionsAdapter, call_llm
from agent.auxiliary_task_config import session_auxiliary_scope


@pytest.mark.parametrize("effort", ["low", "high"])
def test_compression_effort_reaches_responses_and_keeps_summary_uncapped(effort):
    """The operator can lower summary reasoning without changing routes or truncating its output."""
    model = "gpt-6.1-sol"
    base_url = "https://relay.example/backend-api/codex/"
    adapter = _CodexCompletionsAdapter(SimpleNamespace(base_url=base_url), model)
    sent = {}
    response = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content="Full summary with the current task and next step."),
        finish_reason="stop",
    )])

    def create(**kwargs):
        sent.update(adapter._build_responses_kwargs(kwargs)[0])
        return response

    client = SimpleNamespace(
        base_url=base_url,
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    auxiliary = {
        "compression": {"provider": "codex-lb", "model": model, "reasoning_effort": effort},
        "title_generation": {"provider": "codex-lb", "model": model},
    }
    with session_auxiliary_scope(auxiliary), patch(
        "agent.auxiliary_client._get_cached_client", return_value=(client, model),
    ):
        result = call_llm(task="compression", messages=[{"role": "user", "content": "Summarize the task."}])
        compression_request = dict(sent)
        sent.clear()
        call_llm(task="title_generation", messages=[{"role": "user", "content": "Name the task."}])

    assert result.choices[0].message.content == response.choices[0].message.content
    assert compression_request["model"] == model
    assert compression_request["reasoning"]["effort"] == effort
    assert "max_output_tokens" not in compression_request
    assert "reasoning" not in sent  # The compression setting never changes another auxiliary task.
