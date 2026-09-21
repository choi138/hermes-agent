"""Pure tool-call guardrail primitive tests."""

import json
import logging
import itertools

import pytest

from agent.tool_guardrails import (
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    ToolCallSignature,
    canonical_tool_args,
    classify_tool_failure,
)
from hermes_cli.config_defaults import DEFAULT_CONFIG


def test_tool_call_signature_hashes_canonical_nested_unicode_args_without_exposing_raw_args():
    args_a = {
        "z": [{"β": "☤", "a": 1}],
        "a": {"y": 2, "x": "secret-token-value"},
    }
    args_b = {
        "a": {"x": "secret-token-value", "y": 2},
        "z": [{"a": 1, "β": "☤"}],
    }

    assert canonical_tool_args(args_a) == canonical_tool_args(args_b)
    sig_a = ToolCallSignature.from_call("web_search", args_a)
    sig_b = ToolCallSignature.from_call("web_search", args_b)

    assert sig_a == sig_b
    assert len(sig_a.args_hash) == 64
    metadata = sig_a.to_metadata()
    assert metadata == {"tool_name": "web_search", "args_hash": sig_a.args_hash}
    assert "secret-token-value" not in json.dumps(metadata)
    assert "☤" not in json.dumps(metadata)




def test_config_parses_nested_warn_and_hard_stop_thresholds():
    cfg = ToolCallGuardrailConfig.from_mapping(
        {
            "warnings_enabled": False,
            "hard_stop_enabled": True,
            "warn_after": {
                "exact_failure": 3,
                "same_tool_failure": 4,
                "idempotent_no_progress": 5,
            },
            "hard_stop_after": {
                "exact_failure": 6,
                "same_tool_failure": 7,
                "idempotent_no_progress": 8,
            },
        }
    )

    assert cfg.warnings_enabled is False
    assert cfg.hard_stop_enabled is True
    assert cfg.exact_failure_warn_after == 3
    assert cfg.same_tool_failure_warn_after == 4
    assert cfg.no_progress_warn_after == 5
    assert cfg.exact_failure_block_after == 6
    assert cfg.same_tool_failure_halt_after == 7
    assert cfg.no_progress_block_after == 8


def test_default_repeated_identical_failed_call_warns_without_blocking():
    controller = ToolCallGuardrailController()
    args = {"query": "same"}

    decisions = []
    for _ in range(5):
        assert controller.before_call("web_search", args).action == "allow"
        decisions.append(
            controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
        )

    assert decisions[0].action == "allow"
    assert [d.action for d in decisions[1:]] == ["warn", "warn", "warn", "warn"]
    assert {d.code for d in decisions[1:]} == {"repeated_exact_failure_warning"}
    assert controller.before_call("web_search", args).action == "allow"
    assert controller.halt_decision is None


def test_hard_stop_enabled_blocks_repeated_exact_failure_before_next_execution():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_warn_after=2,
            exact_failure_block_after=2,
            same_tool_failure_halt_after=99,
        )
    )
    args = {"query": "same"}

    assert controller.before_call("web_search", args).action == "allow"
    first = controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert first.action == "allow"

    assert controller.before_call("web_search", args).action == "allow"
    second = controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert second.action == "warn"
    assert second.code == "repeated_exact_failure_warning"

    blocked = controller.before_call("web_search", args)
    assert blocked.action == "block"
    assert blocked.code == "repeated_exact_failure_block"
    assert blocked.count == 2














def test_mutating_or_unknown_tools_are_not_blocked_for_repeated_identical_success_output_by_default():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(no_progress_warn_after=2, no_progress_block_after=2)
    )

    for _ in range(3):
        assert controller.before_call("write_file", {"path": "/tmp/x", "content": "x"}).action == "allow"
        assert controller.after_call("write_file", {"path": "/tmp/x", "content": "x"}, "ok", failed=False).action == "allow"
        assert controller.before_call("custom_tool", {"x": 1}).action == "allow"
        assert controller.after_call("custom_tool", {"x": 1}, "ok", failed=False).action == "allow"






# ── Per-turn runaway-loop caps (Claude Code v2.1.212, Week 29) ──────────────

from agent.tool_guardrails import LoopCapConfig  # noqa: E402






def test_loop_cap_zero_disables_and_junk_falls_back():
    # 0 is a legitimate "unlimited" value; negatives / junk fall back to default.
    assert LoopCapConfig.from_mapping({"max_web_searches": 0}).max_web_searches == 0
    assert LoopCapConfig.from_mapping({"max_web_searches": -5}).max_web_searches == 50
    assert LoopCapConfig.from_mapping({"max_subagents": "nope"}).max_subagents == 50


def test_web_search_cap_blocks_after_limit_regardless_of_hard_stop():
    # Loop caps fire even with hard_stop_enabled=False (the per-turn loop
    # detector's flag). Each distinct query avoids the loop detector so we know
    # the block came from the loop cap, not exact-failure repetition.
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=False,
            loop_caps=LoopCapConfig(max_web_searches=3),
        )
    )
    for i in range(3):
        assert controller.before_call("web_search", {"query": f"q{i}"}).action == "allow"
    decision = controller.before_call("web_search", {"query": "q4"})
    assert decision.action == "block"
    assert decision.code == "loop_web_search_cap"
    assert decision.should_halt is True












def test_graphiti_ok_is_advisory():
    controller = ToolCallGuardrailController()
    controller.after_call(
        "search_memory_facts",
        {"query": "historical context"},
        json.dumps({"status": "ok", "fallback_allowed": False}),
        failed=False,
    )

    decision = controller.before_call("web_search", {"query": "verify current source"})
    assert decision.action == "allow", decision.to_metadata()


@pytest.mark.parametrize(
    "status", ["ok", "ok_low_relevance", "empty", "filtered", "timeout", "error", "missing", None]
)
@pytest.mark.parametrize("legacy_fallback", [True, False, None])
@pytest.mark.parametrize("legacy_config", [False, True])
def test_graphiti_status_never_controls_subsequent_permissions(
    status, legacy_fallback, legacy_config
):
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig.from_mapping(
            {}, memory_config={"graphiti": {
                "allow_irrelevant_fallback": legacy_config,
                "irrelevant_fallback_max_per_turn": 1,
            }}
        )
    )
    result = {} if status is None else {"status": status}
    if legacy_fallback is not None:
        result["fallback_allowed"] = legacy_fallback
    controller.after_call("search_memory_facts", {"query": "history"}, json.dumps(result))

    for flag in ({}, {"graphiti_irrelevant": False}, {"graphiti_irrelevant": True}):
        for tool, args in (
            ("web_search", {"query": "verify"}),
            ("web_extract", {"url": "https://example.com"}),
            ("session_search", {"query": "history"}),
            ("session_search", {"session_id": "s", "around_message_id": 1}),
            ("session_search", {"session_id": "s"}),
            ("session_search", {}),
            ("browser_navigate", {"url": "https://example.com"}),
            ("browser_snapshot", {}),
            ("computer_use", {"action": "capture"}),
            ("search_memory_facts", {"query": "refine"}),
            ("read_file", {"path": "/tmp/x"}),
        ):
            decision = controller.before_call(tool, {**args, **flag})
            assert decision.action == "allow", decision.to_metadata()
    controller.reset_for_turn()
    assert controller.before_call("web_search", {"query": "new turn"}).action == "allow"


@pytest.mark.parametrize("enabled, cap", [(False, 1), (True, 1), (True, 3), (True, 0)])
def test_legacy_graphiti_config_and_flag_are_inert(enabled, cap, caplog):
    config = ToolCallGuardrailConfig(
        allow_graphiti_irrelevant_fallback=enabled,
        graphiti_irrelevant_fallback_max_per_turn=cap,
    )
    controller = ToolCallGuardrailController(config)
    controller.after_call("search_memory_facts", {}, '{"status":"ok","fallback_allowed":false}')
    with caplog.at_level(logging.INFO, logger="agent.tool_guardrails"):
        for i in range(6):
            for flag in ({}, {"graphiti_irrelevant": False}, {"graphiti_irrelevant": True}):
                assert controller.before_call(
                    "session_search", {"query": f"topic {i}", **flag}
                ).action == "allow"
    assert "escape hatch" not in caplog.text


def test_legacy_graphiti_config_still_loads_from_existing_defaults():
    config = ToolCallGuardrailConfig.from_mapping(
        DEFAULT_CONFIG["tool_loop_guardrails"], memory_config=DEFAULT_CONFIG["memory"]
    )
    assert config.allow_graphiti_irrelevant_fallback is False
    for value, expected in [(3, 3), (0, 0), (-4, 1), ("bad", 1)]:
        loaded = ToolCallGuardrailConfig.from_mapping(
            {}, memory_config={"graphiti": {"irrelevant_fallback_max_per_turn": value}}
        )
        assert loaded.graphiti_irrelevant_fallback_max_per_turn == expected


@pytest.mark.parametrize("flag", [{}, {"graphiti_irrelevant": False}, {"graphiti_irrelevant": True}])
def test_graphiti_sequential_search_chain_needs_no_escape_hatch(flag):
    controller = ToolCallGuardrailController()
    chain = [
        ("search_memory_facts", {"query": "history"}, '{"status":"ok","fallback_allowed":false}'),
        ("session_search", {"query": "history", **flag}, '{"session_id":"s"}'),
        ("session_search", {"session_id": "s", **flag}, "historical source"),
        ("session_search", {"session_id": "s", "around_message_id": 1, **flag}, "page"),
        ("web_search", {"query": "current source"}, '{"url":"https://example.com"}'),
        ("web_extract", {"url": "https://example.com"}, "current source"),
        ("session_search", {"query": "another topic", **flag}, "another source"),
    ]
    for tool, args, result in chain:
        assert controller.before_call(tool, args).action == "allow"
        controller.after_call(tool, args, result, failed=False)


@pytest.mark.parametrize("completion_order", list(itertools.permutations(("ok", "empty", "error"))))
def test_graphiti_completion_order_cannot_change_permissions(completion_order):
    controller = ToolCallGuardrailController()
    # Calls submitted together may complete in any order.
    for status in completion_order:
        assert controller.before_call("search_memory_facts", {"query": status}).action == "allow"
    for status in completion_order:
        controller.after_call(
            "search_memory_facts", {"query": status},
            json.dumps({"status": status, "fallback_allowed": status != "ok"}),
        )
        assert controller.before_call("web_search", {"query": status}).action == "allow"
        assert controller.before_call("session_search", {"query": status}).action == "allow"


@pytest.mark.parametrize("tool", ["web_search", "search_memory_facts", "session_search"])
@pytest.mark.parametrize("flag", [{}, {"graphiti_irrelevant": False}, {"graphiti_irrelevant": True}])
@pytest.mark.parametrize("failed", [True, False])
def test_advisory_routing_preserves_generic_failure_and_no_progress_blocks(tool, flag, failed):
    controller = ToolCallGuardrailController(ToolCallGuardrailConfig(
        hard_stop_enabled=True, exact_failure_block_after=2,
        same_tool_failure_halt_after=99, no_progress_block_after=2,
    ))
    controller.after_call("search_memory_facts", {"query": "seed"}, '{"status":"ok"}')
    args = {"query": "repeat", **flag}
    result = '{"error":"unavailable"}' if failed else '{"status":"ok","recall":"same"}'
    for _ in range(2):
        assert controller.before_call(tool, args).action == "allow"
        controller.after_call(tool, args, result, failed=failed)
    decision = controller.before_call(tool, args)
    assert decision.action == "block"
    assert decision.code == (
        "repeated_exact_failure_block" if failed else "idempotent_no_progress_block"
    )


def test_advisory_routing_preserves_same_tool_failure_halt_and_loop_cap():
    controller = ToolCallGuardrailController(ToolCallGuardrailConfig(
        hard_stop_enabled=True, same_tool_failure_halt_after=2,
        loop_caps=LoopCapConfig(max_web_searches=1),
    ))
    controller.after_call("search_memory_facts", {}, '{"status":"ok"}')
    assert controller.before_call("web_search", {"query": "first"}).action == "allow"
    assert controller.before_call("web_search", {"query": "second"}).code == "loop_web_search_cap"
    controller.reset_for_turn()
    for i in range(2):
        decision = controller.after_call(
            "search_memory_facts", {"query": str(i)}, '{"error":"unavailable"}', failed=True
        )
    assert decision.action == "halt"
    assert decision.code == "same_tool_failure_halt"


def test_malformed_model_visible_graphiti_result_allows_fallback():
    controller = ToolCallGuardrailController()
    controller.after_call(
        "search_memory_facts",
        {"query": "refined"},
        "not-json",
        failed=True,
    )

    decision = controller.before_call("session_search", {"query": "fallback"})
    assert decision.action == "allow"


def test_search_memory_facts_participates_in_idempotent_no_progress_guard():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(no_progress_warn_after=2)
    )
    args = {"query": "same history"}
    result = json.dumps({"status": "empty", "fallback_allowed": True})

    first = controller.after_call("search_memory_facts", args, result, failed=False)
    second = controller.after_call("search_memory_facts", args, result, failed=False)

    assert first.action == "allow"
    assert second.action == "warn"
    assert second.code == "idempotent_no_progress_warning"
