"""Production-wiring test for the read-only Graphiti memory provider."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import yaml

import tools.mcp_tool as mcp_tool


_ALLOWED_TOOLS = (
    "get_entity_edge",
    "get_status",
    "search_memory_facts",
    "search_nodes",
)


@contextmanager
def _running_mcp_loop(monkeypatch):
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run() -> None:
        asyncio.set_event_loop(loop)
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    assert ready.wait(1)
    monkeypatch.setattr(mcp_tool, "_mcp_loop", loop)
    try:
        yield
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=1)
        loop.close()


class _Session:
    def __init__(self) -> None:
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        return SimpleNamespace(
            isError=False,
            structuredContent={
                "facts": [
                    {
                        "name": "RELATED_TO",
                        "fact": "P1 branch is hermes/all-work.",
                        "created_at": "2026-07-29T00:00:00Z",
                    }
                ]
            },
            content=[],
        )


def _config() -> dict:
    return {
        "url": "http://127.0.0.1:8201/mcp",
        "transport": "streamable_http",
        "enabled": True,
        "model_visible": False,
        "follow_redirects": False,
        "timeout": 2.5,
        "sampling": {"enabled": False},
        "elicitation": {"enabled": False},
        "tools": {
            "include": list(_ALLOWED_TOOLS),
            "exclude": [],
            "resources": False,
            "prompts": False,
        },
    }


def _live_server(profile_home, config, session):
    server = mcp_tool.MCPServerTask("graphiti_canonical")
    server._profile_home = str(profile_home.resolve())
    server._config = config
    server.tool_timeout = 2.5
    server.session = session
    server.initialize_result = SimpleNamespace(capabilities={})
    server._tools = [
        SimpleNamespace(
            name=name,
            description=f"read-only {name}",
            inputSchema={"type": "object", "properties": {}},
            outputSchema=None,
            annotations=None,
        )
        for name in _ALLOWED_TOOLS
    ]
    server._registered_tool_names = mcp_tool._register_server_tools(
        "graphiti_canonical", server, config
    )
    return server


def test_aiagent_loads_filtered_search_wrapper_but_keeps_raw_mcp_tools_hidden(
    tmp_path, monkeypatch
):
    config = _config()
    (tmp_path / "memories").mkdir()
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "memory": {"provider": "graphiti_canonical"},
                "agent": {},
                "mcp_servers": {"graphiti_canonical": config},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    import hermes_cli.config as cli_config
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    if hasattr(cli_config, "get_hermes_home"):
        monkeypatch.setattr(cli_config, "get_hermes_home", lambda: tmp_path)

    session = _Session()
    server = _live_server(tmp_path, config, session)
    with mcp_tool._lock:
        mcp_tool._servers["graphiti_canonical"] = server

    try:
        from model_tools import get_tool_definitions, handle_function_call
        from tools.registry import registry

        search_tool = "mcp__graphiti_canonical__search_memory_facts"
        assert registry.get_entry(search_tool) is not None
        raw_catalog = get_tool_definitions(
            enabled_toolsets=["mcp-graphiti_canonical"],
            quiet_mode=True,
            skip_tool_search_assembly=True,
        )
        assert not any(
            tool.get("function", {}).get("name", "").startswith(
                "mcp__graphiti_canonical__"
            )
            for tool in raw_catalog
            if isinstance(tool, dict)
        )
        with patch.object(
            registry,
            "dispatch",
            side_effect=AssertionError("hidden model call reached registry dispatch"),
        ):
            direct_result = json.loads(handle_function_call(search_tool, {}))
        assert "not available for model dispatch" in direct_result["error"]
        assert session.calls == []

        with (
            patch("agent.model_metadata.get_model_context_length", return_value=204_800),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            from run_agent import AIAgent

            agent = AIAgent(
                api_key="test-key-1234567890",
                base_url="https://openrouter.ai/api/v1",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=False,
                session_id="production-wiring",
            )

        assert agent._memory_manager is not None
        assert [
            schema["name"]
            for schema in agent._memory_manager.get_all_tool_schemas()
        ] == ["search_memory_facts"]
        assert any(
            tool.get("function", {}).get("name") == "search_memory_facts"
            for tool in agent.tools
            if isinstance(tool, dict)
        )
        assert not any(
            tool.get("function", {}).get("name", "").startswith(
                "mcp__graphiti_canonical__"
            )
            for tool in agent.tools
            if isinstance(tool, dict)
        )

        provider = agent._memory_manager._providers[0]
        assert provider.name == "graphiti_canonical"
        with _running_mcp_loop(monkeypatch):
            tool_result = json.loads(
                agent._memory_manager.handle_tool_call(
                    "search_memory_facts",
                    {"query": "continue the previous P1 project"},
                )
            )
            context = provider.prefetch("continue the previous P1 project")

        assert tool_result["status"] == "ok"
        assert "P1 branch is hermes/all-work." in tool_result["recall"]
        assert "P1 branch is hermes/all-work." in context
        assert session.calls == [
            (
                "search_memory_facts",
                {"query": "continue the previous P1 project", "max_facts": 12},
            ),
            (
                "search_memory_facts",
                {"query": "continue the previous P1 project", "max_facts": 12},
            ),
        ]
    finally:
        server._deregister_tools()
        with mcp_tool._lock:
            mcp_tool._servers.pop("graphiti_canonical", None)
