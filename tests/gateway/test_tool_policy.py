from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from gateway.tool_policy import (
    DISCORD_CORE_SCHEMA_BUDGET_BYTES,
    apply_gateway_tool_schema_policy,
    canonical_tool_schema_metrics,
    resolve_gateway_tool_policy,
    schema_budget_bytes,
    schema_within_budget,
)


def _without_descriptions(value):
    if isinstance(value, dict):
        return {
            key: _without_descriptions(item)
            for key, item in value.items()
            if key != "description"
        }
    if isinstance(value, list):
        return [_without_descriptions(item) for item in value]
    return value


def _source(**overrides):
    values = {
        "user_id": "user-1",
        "chat_id": "thread-1",
        "thread_id": "thread-1",
        "parent_chat_id": "channel-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _config(**kanban_overrides):
    kanban = {
        "discord_ops_users": [],
        "discord_ops_channels": [],
    }
    kanban.update(kanban_overrides)
    return {"toolsets": ["kanban"], "kanban": kanban}


def test_normal_discord_replaces_kanban_surface_with_one_submit_toolset():
    policy = resolve_gateway_tool_policy(
        _config(),
        platform="discord",
        source=_source(),
        identity_profile="shinei",
        enabled_toolsets=["messaging", "kanban", "kanban_worker"],
    )

    assert policy.name == "discord-core"
    assert policy.identity_profile == "shinei"
    assert "kanban_submit" in policy.enabled_toolsets
    assert "kanban" not in policy.enabled_toolsets
    assert "kanban_worker" not in policy.enabled_toolsets


def test_discord_ops_requires_exact_user_and_channel_matches():
    config = _config(
        discord_ops_users=["user-1"],
        discord_ops_channels=["channel-1"],
    )
    allowed = resolve_gateway_tool_policy(
        config,
        platform="discord",
        source=_source(),
        identity_profile="default",
        enabled_toolsets=["messaging", "kanban"],
    )
    wrong_user = resolve_gateway_tool_policy(
        config,
        platform="discord",
        source=_source(user_id="user-2"),
        identity_profile="default",
        enabled_toolsets=["messaging", "kanban"],
    )

    assert allowed.name == "discord-ops"
    assert "kanban" in allowed.enabled_toolsets
    assert "kanban_submit" not in allowed.enabled_toolsets
    assert wrong_user.name == "discord-core"
    assert "kanban_submit" in wrong_user.enabled_toolsets
    assert "kanban" not in wrong_user.enabled_toolsets


def test_discord_ops_rejects_wildcard_allowlists():
    policy = resolve_gateway_tool_policy(
        _config(
            discord_ops_users=["*"],
            discord_ops_channels=["channel-1"],
        ),
        platform="discord",
        source=_source(),
        identity_profile="default",
        enabled_toolsets=["messaging", "kanban"],
    )

    assert policy.name == "discord-core"
    assert "kanban_submit" in policy.enabled_toolsets


def test_agent_disabled_kanban_is_a_final_veto():
    policy = resolve_gateway_tool_policy(
        _config(),
        platform="discord",
        source=_source(),
        identity_profile="default",
        enabled_toolsets=["messaging", "kanban"],
        disabled_toolsets=["kanban"],
    )

    assert "kanban" not in policy.enabled_toolsets
    assert "kanban_submit" not in policy.enabled_toolsets


def test_non_discord_policy_is_unchanged():
    policy = resolve_gateway_tool_policy(
        _config(),
        platform="telegram",
        source=_source(),
        identity_profile="default",
        enabled_toolsets=["messaging", "kanban"],
    )

    assert policy.name == "platform-default"
    assert policy.enabled_toolsets == ("kanban", "messaging")


def test_schema_hash_covers_full_canonical_json_not_only_tool_names():
    first = [
        {
            "type": "function",
            "function": {
                "name": "same_name",
                "description": "first",
                "parameters": {"type": "object", "required": ["value"]},
            },
        }
    ]
    same_content_different_key_order = [
        {
            "function": {
                "parameters": {"required": ["value"], "type": "object"},
                "description": "first",
                "name": "same_name",
            },
            "type": "function",
        }
    ]
    changed_description = [
        {
            "type": "function",
            "function": {
                "name": "same_name",
                "description": "changed",
                "parameters": {"type": "object", "required": ["value"]},
            },
        }
    ]

    first_metrics = canonical_tool_schema_metrics(first)
    reordered_metrics = canonical_tool_schema_metrics(same_content_different_key_order)
    changed_metrics = canonical_tool_schema_metrics(changed_description)

    assert first_metrics.schema_hash == reordered_metrics.schema_hash
    assert first_metrics.json_bytes == reordered_metrics.json_bytes
    assert first_metrics.schema_hash != changed_metrics.schema_hash


def test_schema_budget_is_a_discord_core_runtime_gate_only():
    at_budget = canonical_tool_schema_metrics(
        [{"padding": "x" * (DISCORD_CORE_SCHEMA_BUDGET_BYTES - 16)}]
    )
    over_budget = canonical_tool_schema_metrics(
        [{"padding": "x" * DISCORD_CORE_SCHEMA_BUDGET_BYTES}]
    )

    assert schema_budget_bytes("discord-core") == 50_000
    assert schema_budget_bytes("discord-ops") is None
    assert schema_within_budget("discord-core", at_budget)
    assert not schema_within_budget("discord-core", over_budget)
    assert schema_within_budget("discord-ops", over_budget)


def test_discord_core_compaction_preserves_every_non_description_contract():
    from tools.cronjob_tools import CRONJOB_SCHEMA
    from tools.session_search_tool import SESSION_SEARCH_SCHEMA
    from tools.skill_manager_tool import SKILL_MANAGE_SCHEMA
    from tools.terminal_tool import TERMINAL_SCHEMA

    schemas = [
        {"type": "function", "function": schema}
        for schema in (
            CRONJOB_SCHEMA,
            TERMINAL_SCHEMA,
            SESSION_SEARCH_SCHEMA,
            SKILL_MANAGE_SCHEMA,
        )
    ]
    original = deepcopy(schemas)

    compacted = apply_gateway_tool_schema_policy("discord-core", schemas)

    # Global registry/cache objects are untouched, and all authorization /
    # validation semantics (names, properties, required, enums, defaults,
    # bounds) remain byte-equivalent after descriptions are removed.
    assert schemas == original
    assert _without_descriptions(compacted) == _without_descriptions(original)
    assert canonical_tool_schema_metrics(compacted).json_bytes <= 12_000

    # Full schemas remain available to authorized operator sessions.
    assert apply_gateway_tool_schema_policy("discord-ops", schemas) == schemas


def test_gateway_agent_with_cron_active_applies_final_discord_budget(monkeypatch):
    """Reproduce the Gateway-only 31st-tool path before measuring budget."""
    import model_tools
    from gateway.run import GatewayRunner
    from tools.registry import invalidate_check_fn_cache

    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    invalidate_check_fn_cache()
    model_tools._clear_tool_defs_cache()
    try:
        raw_tools = model_tools.get_tool_definitions(
            enabled_toolsets=["hermes-discord", "kanban_submit"],
            quiet_mode=True,
        )
        assert "cronjob" in {
            tool["function"]["name"] for tool in raw_tools
        }

        agent = SimpleNamespace(tools=raw_tools)
        policy = SimpleNamespace(name="discord-core", identity_profile="default")
        metrics = GatewayRunner._record_gateway_tool_policy(agent, policy)

        assert metrics == canonical_tool_schema_metrics(agent.tools)
        assert metrics.json_bytes <= DISCORD_CORE_SCHEMA_BUDGET_BYTES
    finally:
        # Availability is process-global and TTL-cached; never leak the
        # gateway-session verdict into worker-policy tests in this file.
        invalidate_check_fn_cache()
        model_tools._clear_tool_defs_cache()


def test_submit_and_worker_toolsets_have_disjoint_lifecycle_contract(monkeypatch):
    import model_tools

    model_tools._clear_tool_defs_cache()
    submit_names = {
        definition["function"]["name"]
        for definition in model_tools.get_tool_definitions(
            enabled_toolsets=["kanban_submit"],
            quiet_mode=True,
        )
    }
    assert submit_names == {"kanban_task"}

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    model_tools._clear_tool_defs_cache()
    worker_names = {
        definition["function"]["name"]
        for definition in model_tools.get_tool_definitions(
            enabled_toolsets=["kanban_worker"],
            quiet_mode=True,
        )
    }
    assert worker_names == {
        "kanban_show",
        "kanban_complete",
        "kanban_block",
        "kanban_heartbeat",
        "kanban_comment",
        "kanban_create",
        "kanban_link",
    }
    assert not worker_names & {"kanban_list", "kanban_unblock", "kanban_task"}


def test_worker_env_narrows_inherited_orchestrator_and_intake_toolsets(monkeypatch):
    import model_tools

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    model_tools._clear_tool_defs_cache()
    worker_names = {
        definition["function"]["name"]
        for definition in model_tools.get_tool_definitions(
            enabled_toolsets=["kanban", "kanban_submit"],
            disabled_toolsets=["kanban_worker"],
            quiet_mode=True,
        )
    }

    assert worker_names == {
        "kanban_show",
        "kanban_complete",
        "kanban_block",
        "kanban_heartbeat",
        "kanban_comment",
        "kanban_create",
        "kanban_link",
    }
