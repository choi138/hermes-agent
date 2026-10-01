"""Removal contract: no executable feature surface or agent signature remains."""
from dataclasses import fields
import importlib
import inspect

import pytest

from gateway.run_startup import GatewayStartupMixin
from gateway.run_turn import GatewayTurnMixin
from gateway.turn_context import TurnContext
from plugins.platforms.discord.adapter import DiscordAdapter


@pytest.mark.parametrize("module", [
    "plugins.mention_inbox",
    "gateway.run_mention_inbox",
    "plugins.platforms.discord.mention_inbox_adapter",
])
def test_dedicated_feature_modules_are_unavailable(module):
    with pytest.raises(ModuleNotFoundError) as error:
        importlib.import_module(module)
    assert error.value.name == module


def test_no_feature_fields_hooks_or_call_signatures():
    assert not any("mention_inbox" in field.name for field in fields(TurnContext))
    for cls in (GatewayStartupMixin, DiscordAdapter):
        assert not any("mention_inbox" in name for name in dir(cls))
    for method in (GatewayTurnMixin._run_agent_inner, GatewayTurnMixin._run_agent_via_proxy):
        assert not any("mention_inbox" in name for name in inspect.signature(method).parameters)
