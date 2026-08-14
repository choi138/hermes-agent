from gateway.agent_health import UpstreamFailureTracker, classify_log_record


def test_stream_stale_is_allowlisted_and_requires_three_samples():
    message = (
        "Stream stale for 300s (threshold 300s) — no chunks received. "
        "model=test context=~1,000 tokens. Killing connection."
    )
    event = classify_log_record("agent.chat_completion_helpers", message)
    assert event is not None
    tracker = UpstreamFailureTracker(threshold=3, window_seconds=600)
    assert tracker.record(event, now=0) is None
    assert tracker.record(event, now=100) is None
    aggregate = tracker.record(event, now=200)
    assert aggregate is not None
    assert aggregate.rule == "C3.upstream_failure_streak"
    assert aggregate.mention is True


def test_api_retry_exhaustion_is_allowlisted_terminal_error():
    event = classify_log_record(
        "agent.conversation_loop",
        "[abc] API call failed after 5 retries. APIConnectionError | provider=x",
    )
    assert event is not None
    assert event.rule == "B.api_retries_exhausted"
    assert event.mention is False


def test_graphiti_park_and_recovery_are_allowlisted():
    parked = classify_log_record(
        "tools.mcp_tool",
        "MCP server 'memory-server-graphiti-mcp-1': failed after 5 "
        "reconnection attempts, parking (state: degraded → parked): x",
    )
    recovered = classify_log_record(
        "tools.mcp_tool",
        "MCP server 'memory-server-graphiti-mcp-1': revived — session healthy "
        "again after parking (state: parked → connected)",
    )
    assert parked is not None and parked.rule == "C4.graphiti_parked"
    assert recovered is not None and recovered.rule == "C4.graphiti_recovered"


def test_graphiti_initial_connection_park_is_allowlisted():
    event = classify_log_record(
        "tools.mcp_tool",
        "MCP server 'graphiti_canonical' failed initial connection after "
        "3 attempts, parking until a reconnect is requested "
        "(state: connecting → parked): ReadError",
    )

    assert event is not None
    assert event.rule == "C4.graphiti_parked"
    assert event.mention is True


def test_routine_tool_terminal_warning_is_never_promoted():
    assert classify_log_record(
        "tools.terminal_tool", "Tool terminal returned error: exit status 1"
    ) is None


def test_runtime_cwd_warning_is_never_promoted():
    assert classify_log_record(
        "agent.runtime_cwd", "TERMINAL_CWD does not exist: /tmp/missing"
    ) is None


def test_same_text_from_wrong_logger_is_not_promoted():
    assert classify_log_record(
        "some.other.logger",
        "Stream stale for 300s — no chunks received. model=x",
    ) is None
