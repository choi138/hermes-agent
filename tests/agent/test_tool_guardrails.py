"""Pure tool-call guardrail primitive tests."""

import json
import logging

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












def test_graphiti_ok_blocks_external_fallback_but_non_ok_statuses_allow_it():
    controller = ToolCallGuardrailController()
    controller.set_graphiti_routing_status("ok")

    for tool_name in (
        "web_search",
        "web_extract",
        "session_search",
        "browser_navigate",
        "browser_snapshot",
        "computer_use",
    ):
        decision = controller.before_call(tool_name, {"query": "fallback"})
        assert decision.action == "deny"
        assert decision.should_halt is False
        assert decision.code == "graphiti_fallback_not_allowed"
        assert "ok" in decision.message

    assert controller.before_call("search_memory_facts", {"query": "refine"}).action == "allow"
    assert controller.before_call("read_file", {"path": "/tmp/x"}).action == "allow"

    for status in ("empty", "filtered", "timeout", "error", "missing"):
        controller.set_graphiti_routing_status(status)
        assert controller.before_call("web_search", {"query": status}).action == "allow"

    controller.reset_for_turn()
    assert controller.before_call("web_search", {"query": "new turn"}).action == "allow"


def test_graphiti_irrelevant_fallback_config_defaults_off_and_reads_memory_key():
    default_cfg = ToolCallGuardrailConfig.from_mapping(
        DEFAULT_CONFIG["tool_loop_guardrails"],
        memory_config=DEFAULT_CONFIG["memory"],
    )
    enabled_cfg = ToolCallGuardrailConfig.from_mapping(
        {},
        memory_config={"graphiti": {"allow_irrelevant_fallback": True}},
    )

    assert default_cfg.allow_graphiti_irrelevant_fallback is False
    assert enabled_cfg.allow_graphiti_irrelevant_fallback is True


def test_graphiti_irrelevant_fallback_budget_defaults_to_one_and_parses_override():
    default_cfg = ToolCallGuardrailConfig.from_mapping(
        DEFAULT_CONFIG["tool_loop_guardrails"],
        memory_config=DEFAULT_CONFIG["memory"],
    )
    override_cfg = ToolCallGuardrailConfig.from_mapping(
        {},
        memory_config={
            "graphiti": {
                "allow_irrelevant_fallback": True,
                "irrelevant_fallback_max_per_turn": 3,
            }
        },
    )
    unlimited_cfg = ToolCallGuardrailConfig.from_mapping(
        {},
        memory_config={"graphiti": {"irrelevant_fallback_max_per_turn": 0}},
    )
    junk_cfg = ToolCallGuardrailConfig.from_mapping(
        {},
        memory_config={"graphiti": {"irrelevant_fallback_max_per_turn": -4}},
    )

    assert default_cfg.graphiti_irrelevant_fallback_max_per_turn == 1
    assert override_cfg.graphiti_irrelevant_fallback_max_per_turn == 3
    # 0 is a legitimate "unlimited" value; negatives fall back to the default.
    assert unlimited_cfg.graphiti_irrelevant_fallback_max_per_turn == 0
    assert junk_cfg.graphiti_irrelevant_fallback_max_per_turn == 1


def test_graphiti_ok_allows_flagged_session_search_when_escape_hatch_enabled(caplog):
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(allow_graphiti_irrelevant_fallback=True)
    )
    controller.set_graphiti_routing_status("ok")

    with caplog.at_level(logging.INFO, logger="agent.tool_guardrails"):
        decision = controller.before_call(
            "session_search",
            {"query": "actual preference", "graphiti_irrelevant": True},
        )

    assert decision.action == "allow"
    assert "tool=session_search" in caplog.text
    assert "Graphiti status=ok" in caplog.text


def test_graphiti_ok_denies_flagged_session_search_when_escape_hatch_disabled():
    controller = ToolCallGuardrailController()
    controller.set_graphiti_routing_status("ok")

    decision = controller.before_call(
        "session_search",
        {"query": "actual preference", "graphiti_irrelevant": True},
    )

    assert decision.action == "deny"
    assert decision.code == "graphiti_fallback_not_allowed"


def test_graphiti_irrelevant_flag_does_not_bypass_other_fallback_tools():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(allow_graphiti_irrelevant_fallback=True)
    )
    controller.set_graphiti_routing_status("ok")

    decision = controller.before_call(
        "web_search",
        {"query": "actual preference", "graphiti_irrelevant": True},
    )

    assert decision.action == "deny"
    assert decision.code == "graphiti_fallback_not_allowed"


def test_graphiti_flagged_discovery_is_budgeted_one_per_turn_by_default():
    # A second *flagged discovery* still costs a budget slot and is refused at
    # the default budget of 1 — the Graphiti-first default stays strict.
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(allow_graphiti_irrelevant_fallback=True)
    )
    controller.set_graphiti_routing_status("ok")

    first = controller.before_call(
        "session_search", {"query": "actual preference", "graphiti_irrelevant": True}
    )
    assert first.action == "allow"

    second = controller.before_call(
        "session_search", {"query": "a different topic", "graphiti_irrelevant": True}
    )
    assert second.action == "deny"
    assert second.code == "graphiti_fallback_not_allowed"
    assert "already spent for this turn" in second.message

    controller.reset_for_turn()
    controller.set_graphiti_routing_status("ok")
    assert (
        controller.before_call(
            "session_search", {"query": "new turn", "graphiti_irrelevant": True}
        ).action
        == "allow"
    )


def test_graphiti_flagged_discovery_budget_is_configurable():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            allow_graphiti_irrelevant_fallback=True,
            graphiti_irrelevant_fallback_max_per_turn=3,
        )
    )
    controller.set_graphiti_routing_status("ok")

    for i in range(3):
        decision = controller.before_call(
            "session_search", {"query": f"topic {i}", "graphiti_irrelevant": True}
        )
        assert decision.action == "allow", i

    assert (
        controller.before_call(
            "session_search", {"query": "topic 4", "graphiti_irrelevant": True}
        ).action
        == "deny"
    )


def test_graphiti_flagged_discovery_budget_zero_means_unlimited():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            allow_graphiti_irrelevant_fallback=True,
            graphiti_irrelevant_fallback_max_per_turn=0,
        )
    )
    controller.set_graphiti_routing_status("ok")

    for i in range(5):
        decision = controller.before_call(
            "session_search", {"query": f"topic {i}", "graphiti_irrelevant": True}
        )
        assert decision.action == "allow", i


# ── Regression: the three observed denials (flagged discovery + follow-up reads)


def test_graphiti_scroll_and_read_follow_ups_allowed_after_flagged_discovery():
    """The core reported defect.

    Flagged discovery was allowed, then every follow-up read of *that same
    session* was denied because the one-shot budget was already spent. Paging
    into an already-permitted session is a continuation, not a new fallback
    source, so it must not consume or be blocked by the budget.
    """
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(allow_graphiti_irrelevant_fallback=True)
    )
    controller.set_graphiti_routing_status("ok")

    assert (
        controller.before_call(
            "session_search", {"query": "auth refactor", "graphiti_irrelevant": True}
        ).action
        == "allow"
    )

    # Scroll shape, flagged and unflagged, repeatedly — all follow-up paging.
    for message_id in (4211, 4231, 4251):
        flagged = controller.before_call(
            "session_search",
            {
                "session_id": "20260824_x",
                "around_message_id": message_id,
                "graphiti_irrelevant": True,
            },
        )
        assert flagged.action == "allow", message_id

        unflagged = controller.before_call(
            "session_search",
            {"session_id": "20260824_x", "around_message_id": message_id},
        )
        assert unflagged.action == "allow", message_id

    # Read shape (session_id only) is a continuation too.
    assert (
        controller.before_call("session_search", {"session_id": "20260824_x"}).action
        == "allow"
    )

    # A brand-new flagged *discovery* is still refused — budget stays spent.
    assert (
        controller.before_call(
            "session_search", {"query": "unrelated topic", "graphiti_irrelevant": True}
        ).action
        == "deny"
    )


def test_graphiti_scroll_denied_when_hatch_never_engaged():
    # Without an allowed flagged discovery this turn, a session_id-shaped call
    # is not a continuation of anything and stays denied.
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(allow_graphiti_irrelevant_fallback=True)
    )
    controller.set_graphiti_routing_status("ok")

    decision = controller.before_call(
        "session_search", {"session_id": "20260824_x", "around_message_id": 4211}
    )
    assert decision.action == "deny"
    assert decision.code == "graphiti_fallback_not_allowed"


def test_graphiti_continuation_allowance_does_not_survive_turn_reset():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(allow_graphiti_irrelevant_fallback=True)
    )
    controller.set_graphiti_routing_status("ok")

    assert (
        controller.before_call(
            "session_search", {"query": "auth refactor", "graphiti_irrelevant": True}
        ).action
        == "allow"
    )
    scroll = {"session_id": "20260824_x", "around_message_id": 4211}
    assert controller.before_call("session_search", scroll).action == "allow"

    controller.reset_for_turn()
    controller.set_graphiti_routing_status("ok")
    assert controller.before_call("session_search", scroll).action == "deny"


def test_graphiti_scroll_denied_when_escape_hatch_disabled():
    # Default config (hatch off): unchanged strict behavior for every shape.
    controller = ToolCallGuardrailController()
    controller.set_graphiti_routing_status("ok")

    for args in (
        {"query": "q", "graphiti_irrelevant": True},
        {"session_id": "s", "around_message_id": 1},
        {"session_id": "s", "around_message_id": 1, "graphiti_irrelevant": True},
        {"session_id": "s"},
    ):
        decision = controller.before_call("session_search", args)
        assert decision.action == "deny", args
        assert decision.code == "graphiti_fallback_not_allowed"
        # The hatch is off, so the denial must not advertise a spent budget.
        assert "already spent" not in decision.message


def test_graphiti_browser_navigate_stays_denied_even_after_hatch_engaged():
    # browser_* is never covered by the escape hatch: the flag is a
    # session_search-only signal and browser tools are a genuinely different
    # source, not a continuation of session history.
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(allow_graphiti_irrelevant_fallback=True)
    )
    controller.set_graphiti_routing_status("ok")

    assert (
        controller.before_call(
            "session_search", {"query": "auth refactor", "graphiti_irrelevant": True}
        ).action
        == "allow"
    )

    for tool_name, args in (
        ("browser_navigate", {"url": "https://example.com"}),
        ("browser_navigate", {"url": "https://example.com", "graphiti_irrelevant": True}),
        ("web_search", {"query": "x", "graphiti_irrelevant": True}),
        ("web_extract", {"url": "https://example.com"}),
        ("computer_use", {"action": "capture", "graphiti_irrelevant": True}),
    ):
        decision = controller.before_call(tool_name, args)
        assert decision.action == "deny", (tool_name, args)
        assert decision.code == "graphiti_fallback_not_allowed"


def test_graphiti_falsey_or_non_session_id_shapes_are_not_continuations():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(allow_graphiti_irrelevant_fallback=True)
    )
    controller.set_graphiti_routing_status("ok")

    assert (
        controller.before_call(
            "session_search", {"query": "auth refactor", "graphiti_irrelevant": True}
        ).action
        == "allow"
    )

    # Browse shape (no args) and empty/non-string session_id are not
    # continuations — they can start a fresh unflagged search.
    for args in ({}, {"session_id": ""}, {"session_id": None}, {"session_id": 123}):
        decision = controller.before_call("session_search", args)
        assert decision.action == "deny", args
        assert decision.code == "graphiti_fallback_not_allowed"


def test_graphiti_non_ok_status_allows_every_session_search_shape():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(allow_graphiti_irrelevant_fallback=True)
    )

    for status in ("empty", "filtered", "timeout", "error", "missing"):
        controller.set_graphiti_routing_status(status)
        assert (
            controller.before_call(
                "session_search", {"session_id": "s", "around_message_id": 1}
            ).action
            == "allow"
        ), status

    # None of that consumed the budget, so a flagged call under status=ok
    # still has its full allowance.
    controller.set_graphiti_routing_status("ok")
    assert (
        controller.before_call(
            "session_search", {"query": "q", "graphiti_irrelevant": True}
        ).action
        == "allow"
    )


def test_graphiti_empty_allows_flagged_session_search_without_consuming_bypass():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(allow_graphiti_irrelevant_fallback=True)
    )
    args = {"query": "actual preference", "graphiti_irrelevant": True}
    controller.set_graphiti_routing_status("empty")

    assert controller.before_call("session_search", args).action == "allow"

    controller.set_graphiti_routing_status("ok")
    assert controller.before_call("session_search", args).action == "allow"


def test_model_visible_graphiti_result_blocks_fallback_only_for_ok_recall():
    controller = ToolCallGuardrailController()
    controller.set_graphiti_routing_status("empty")

    controller.after_call(
        "search_memory_facts",
        {"query": "refined"},
        json.dumps({"status": "ok", "fallback_allowed": False}),
        failed=False,
    )
    blocked = controller.before_call("web_search", {"query": "must not run"})
    assert blocked.action == "deny"
    assert blocked.should_halt is False
    assert "ok" in blocked.message

    controller.after_call(
        "search_memory_facts",
        {"query": "refined"},
        json.dumps({"status": "empty", "fallback_allowed": True}),
        failed=False,
    )
    assert controller.before_call("web_search", {"query": "now allowed"}).action == "allow"


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
