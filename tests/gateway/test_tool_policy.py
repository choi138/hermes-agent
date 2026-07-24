from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

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

    assert schema_budget_bytes("discord-core") == 40_000
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
    assert apply_gateway_tool_schema_policy("discord-core", compacted) == compacted

    # Full schemas remain available to authorized operator sessions.
    assert apply_gateway_tool_schema_policy("discord-ops", schemas) == schemas


def test_real_discord_core_surface_stays_within_40k_without_losing_contracts(
    monkeypatch,
):
    """Exercise a representative 32-tool Discord surface and its final policy."""
    import importlib

    from tools import tts_tool

    import model_tools
    from gateway.run import GatewayRunner
    from tools import discord_tool
    from tools.registry import invalidate_check_fn_cache

    registry_module = importlib.import_module("tools.registry")
    real_check = registry_module._check_fn_cached
    representative_available = {
        "check_browser_requirements",
        "check_browser_vision_requirements",
        "check_computer_use_requirements",
        "check_vision_requirements",
        tts_tool.check_tts_requirements.__name__,
    }

    def representative_check(check_fn):
        if check_fn.__name__ in representative_available:
            return True
        return real_check(check_fn)

    monkeypatch.setattr(registry_module, "_check_fn_cached", representative_check)
    monkeypatch.setattr(
        discord_tool,
        "_get_bot_token",
        lambda: "representative-token",
    )
    monkeypatch.setattr(
        discord_tool,
        "_detect_capabilities_nonblocking",
        lambda _token: {
            "detected": True,
            "has_members_intent": True,
            "has_message_content": False,
        },
    )
    monkeypatch.setattr(discord_tool, "_load_allowed_actions_config", lambda: None)
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    invalidate_check_fn_cache()
    model_tools._clear_tool_defs_cache()
    try:
        raw_tools = model_tools.get_tool_definitions(
            enabled_toolsets=["hermes-discord", "kanban_submit", "tts"],
            quiet_mode=True,
        )
        raw_names = [tool["function"]["name"] for tool in raw_tools]
        raw_tools_original = deepcopy(raw_tools)
        assert len(raw_names) == 32
        assert raw_names.count("kanban_task") == 1
        assert {
            "terminal",
            "process",
            "read_file",
            "write_file",
            "patch",
            "search_files",
            "browser_navigate",
            "browser_click",
            "computer_use",
            "delegate_task",
            "discord",
            "discord_admin",
            "cronjob",
            "kanban_task",
            "text_to_speech",
        } <= set(raw_names)

        agent = SimpleNamespace(tools=raw_tools)
        policy = SimpleNamespace(name="discord-core", identity_profile="default")
        metrics = GatewayRunner._record_gateway_tool_policy(agent, policy)

        assert metrics == canonical_tool_schema_metrics(agent.tools)
        assert metrics.count == 32
        assert raw_tools == raw_tools_original
        assert _without_descriptions(agent.tools) == _without_descriptions(
            raw_tools_original
        )

        final_by_name = {
            tool["function"]["name"]: tool["function"] for tool in agent.tools
        }
        raw_by_name = {
            tool["function"]["name"]: tool["function"]
            for tool in raw_tools_original
        }
        assert final_by_name["terminal"]["parameters"]["required"] == ["command"]
        assert final_by_name["write_file"]["parameters"]["required"] == [
            "path",
            "content",
        ]
        kanban_function = final_by_name["kanban_task"]
        kanban_parameters = kanban_function["parameters"]
        # Create and task-targeting operations cannot share an unconditional
        # required list.  The final Discord schema must retain the concise
        # operation-aware contract instead of resurrecting create-only shape.
        assert "required" not in kanban_parameters
        assert kanban_parameters["properties"]["operation"] == {
            "type": "string",
            "enum": ["create", "status", "update", "retry"],
            "default": "create",
        }
        assert "Create needs title" in kanban_function["description"]
        assert "status/update/retry need task_id" in kanban_function["description"]
        assert "update allows title/body/priority" in kanban_function["description"]
        assert final_by_name["patch"]["parameters"]["properties"]["mode"][
            "enum"
        ] == ["replace", "patch"]
        for property_name in ("tasks", "role"):
            assert final_by_name["delegate_task"]["parameters"]["properties"][
                property_name
            ]["description"] == raw_by_name["delegate_task"]["parameters"][
                "properties"
            ][property_name]["description"]
        for tool_name in ("discord", "discord_admin"):
            assert (
                final_by_name[tool_name]["description"]
                == raw_by_name[tool_name]["description"]
            )

        write_safety = final_by_name["write_file"]["parameters"]["properties"][
            "cross_profile"
        ]["description"]
        write_overwrite_warning = final_by_name["write_file"]["description"]
        patch_safety = final_by_name["patch"]["parameters"]["properties"][
            "cross_profile"
        ]["description"]
        skill_safety = final_by_name["skill_manage"]["description"]
        computer_action_safety = final_by_name["computer_use"]["parameters"][
            "properties"
        ]["action"]["description"]
        computer_focus_safety = final_by_name["computer_use"]["parameters"][
            "properties"
        ]["raise_window"]["description"]
        discord_intent_warning = final_by_name["discord"]["description"]
        discord_permission_warning = final_by_name["discord_admin"]["description"]
        assert "explicit user direction" in write_safety
        assert "blocked with a warning" in write_safety
        assert "OVERWRITES the entire file" in write_overwrite_warning
        assert "explicit user direction" in patch_safety
        assert "Confirm before create/delete" in skill_safety
        assert "requires approval" in computer_action_safety
        assert "DISRUPTS the user" in computer_focus_safety
        assert "MESSAGE_CONTENT privileged intent" in discord_intent_warning
        assert "per-guild permission" in discord_permission_warning
        assert "MANAGE_ROLES" in discord_permission_warning

        assert metrics.json_bytes <= DISCORD_CORE_SCHEMA_BUDGET_BYTES, metrics
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


def _provider_stop_response(text="done"):
    message = SimpleNamespace(
        content=text,
        reasoning_content=None,
        reasoning=None,
        tool_calls=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model",
        usage=None,
    )


@pytest.mark.parametrize(
    ("role", "platform", "ops_allowed", "worker", "expected_names"),
    [
        (
            "research-equivalent",
            "discord",
            False,
            False,
            {"kanban_task"},
        ),
        (
            "coordinator-equivalent",
            "discord",
            True,
            False,
            {
                "kanban_show",
                "kanban_list",
                "kanban_complete",
                "kanban_block",
                "kanban_heartbeat",
                "kanban_comment",
                "kanban_create",
                "kanban_link",
                "kanban_unblock",
                "kanban_attach",
                "kanban_attach_url",
                "kanban_attachments",
            },
        ),
        (
            "coder-equivalent",
            "cli",
            False,
            True,
            {
                "kanban_show",
                "kanban_complete",
                "kanban_block",
                "kanban_heartbeat",
                "kanban_comment",
                "kanban_create",
                "kanban_link",
            },
        ),
    ],
)
def test_role_tool_policy_reaches_stable_provider_request_schema(
    monkeypatch,
    tmp_path,
    role,
    platform,
    ops_allowed,
    worker,
    expected_names,
):
    """Cross the real gateway policy -> AIAgent -> provider-request boundary."""
    import model_tools
    from gateway.run import GatewayRunner
    from hermes_cli import config as config_mod
    from hermes_state import SessionDB
    from run_agent import AIAgent
    from tools.registry import invalidate_check_fn_cache

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    if worker:
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_boundary")
        monkeypatch.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    else:
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        monkeypatch.delenv("HERMES_KANBAN_STOP_NUDGE", raising=False)

    config = {
        "toolsets": ["kanban"],
        "kanban": {
            "discord_ops_users": ["user-1"] if ops_allowed else [],
            "discord_ops_channels": ["channel-1"] if ops_allowed else [],
        },
        "compression": {"enabled": False},
        "prompt_caching": {"cache_ttl": "5m"},
        "sessions": {},
        "bedrock": {},
    }
    monkeypatch.setattr(config_mod, "load_config", lambda: config)
    policy = resolve_gateway_tool_policy(
        config,
        platform=platform,
        source=_source(),
        identity_profile=role,
        enabled_toolsets=(
            ["kanban", "kanban_submit"] if worker else ["kanban"]
        ),
    )

    invalidate_check_fn_cache()
    model_tools._clear_tool_defs_cache()
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        _provider_stop_response("first"),
        _provider_stop_response("second"),
    ]
    try:
        with patch("run_agent.OpenAI", return_value=client):
            agent = AIAgent(
                base_url="https://openrouter.ai/api/v1",
                api_key="test-key",
                model="test/model",
                enabled_toolsets=list(policy.enabled_toolsets),
                disabled_toolsets=[],
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                session_db=SessionDB(db_path=tmp_path / "state.db"),
                session_id=f"policy-boundary-{role}",
                platform=platform,
            )
        recorded_metrics = GatewayRunner._record_gateway_tool_policy(agent, policy)
        agent._disable_streaming = True
        agent._use_prompt_caching = False
        agent.tool_delay = 0
        agent.save_trajectories = False

        with (
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            first = agent.run_conversation("first request", conversation_history=[])
            second = agent.run_conversation("second request", conversation_history=[])

        assert first["completed"] is True
        assert second["completed"] is True
        assert client.chat.completions.create.call_count == 2
        first_request = client.chat.completions.create.call_args_list[0].kwargs
        second_request = client.chat.completions.create.call_args_list[1].kwargs
        first_tools = first_request["tools"]
        second_tools = second_request["tools"]
        assert {tool["function"]["name"] for tool in first_tools} == expected_names
        assert first_tools == second_tools == agent.tools
        assert canonical_tool_schema_metrics(first_tools).schema_hash == (
            recorded_metrics.schema_hash
        )
        assert canonical_tool_schema_metrics(second_tools).schema_hash == (
            recorded_metrics.schema_hash
        )
        assert first_request["messages"][0] == second_request["messages"][0]
    finally:
        invalidate_check_fn_cache()
        model_tools._clear_tool_defs_cache()
