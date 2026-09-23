"""Immutable, profile-bound read-only MCP capability for host-side memory providers."""

from __future__ import annotations

import asyncio
import copy
import concurrent.futures
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List
from urllib.parse import unquote, urlparse

from tools import mcp_tool as _core
from tools.mcp_tool_loop import _wrap_with_home_override, _wrap_with_dashboard_oauth_flow
from tools.mcp_tool_schema import mcp_prefixed_tool_name, sanitize_mcp_name_component
from tools.mcp_tool_scope import _resolve_server_key, _key_scope

_lock = _core._lock
logger = logging.getLogger(__name__)


def _run_on_mcp_loop(coro_or_factory, timeout: float = 30):
    """Schedule a coroutine on the MCP event loop and block until done.

    Accepts either a coroutine object or a zero-arg callable that returns one.
    Callers can pass a factory to avoid constructing coroutine objects when
    the MCP loop is unavailable (which would otherwise leak the coroutine
    frame and emit ``"coroutine was never awaited"`` warnings).

    Poll in short intervals so the calling agent thread can honor user
    interrupts while the MCP work is still running on the background loop.
    """
    from tools.interrupt import is_interrupted
    from agent.async_utils import safe_schedule_threadsafe

    start_time = time.monotonic()
    timeout_value = None if timeout is None else max(0.0, float(timeout))
    hard_deadline = (
        None if timeout_value is None else start_time + timeout_value
    )
    with _lock:
        loop = _core._mcp_loop
    if loop is None or not loop.is_running():
        if asyncio.iscoroutine(coro_or_factory):
            coro_or_factory.close()
        raise RuntimeError("MCP event loop is not running")

    owned_coroutines = []

    def _own(candidate):
        if asyncio.iscoroutine(candidate) and all(
            candidate is not existing for existing in owned_coroutines
        ):
            owned_coroutines.append(candidate)
        return candidate

    def _close_owned_coroutines() -> None:
        for owned in reversed(owned_coroutines):
            owned.close()

    coro = _own(coro_or_factory() if callable(coro_or_factory) else coro_or_factory)

    # Propagate the context-local HERMES_HOME override onto the MCP loop.
    # Tasks scheduled via run_coroutine_threadsafe are created INSIDE the
    # loop thread, so they copy the loop thread's context — not the
    # scheduling thread's. A per-request profile scope (the dashboard's
    # ?profile= endpoints, e.g. the MCP "Test server" probe) would silently
    # vanish here: OAuth token stores and any other get_hermes_home()
    # resolution inside the coroutine would read the process home instead
    # of the selected profile's. Re-establish the override inside the
    # task's own context (task-local — concurrent calls carrying different
    # scopes don't interfere). No-op when no override is active.
    coro = _own(_wrap_with_home_override(coro))
    coro = _own(_wrap_with_dashboard_oauth_flow(coro))
    cleanup_done = threading.Event()

    async def _tracked_coro():
        try:
            return await coro
        finally:
            cleanup_done.set()

    scheduled_coro = _own(_tracked_coro())

    future = safe_schedule_threadsafe(
        scheduled_coro, loop,
        logger=logger,
        log_message="MCP scheduling failed",
    )
    if future is None:
        _close_owned_coroutines()
        raise RuntimeError("MCP event loop unavailable (failed to schedule)")
    cleanup_reserve = (
        0.05
        if timeout_value is None
        else min(0.05, timeout_value * 0.25)
    )
    operation_deadline = (
        None if hard_deadline is None else hard_deadline - cleanup_reserve
    )

    def _close_if_scheduler_abandoned() -> None:
        if (
            not cleanup_done.is_set()
            and getattr(scheduled_coro, "cr_frame", None) is None
        ):
            _close_owned_coroutines()

    while True:
        if is_interrupted():
            future.cancel()
            cleanup_done.wait(cleanup_reserve)
            _close_if_scheduler_abandoned()
            raise InterruptedError("User sent a new message")

        wait_timeout = 0.1
        if operation_deadline is not None:
            remaining = operation_deadline - time.monotonic()
            if remaining <= 0:
                future.cancel()
                cleanup_remaining = max(0.0, hard_deadline - time.monotonic())
                if cleanup_remaining:
                    cleanup_done.wait(cleanup_remaining)
                _close_if_scheduler_abandoned()
                elapsed = time.monotonic() - start_time
                raise TimeoutError(
                    f"MCP call timed out after {elapsed:.1f}s "
                    f"(configured timeout: {timeout_value:.1f}s)"
                )
            wait_timeout = min(wait_timeout, remaining)

        try:
            result = future.result(timeout=wait_timeout)
        except concurrent.futures.TimeoutError:
            # On supported Python versions, concurrent.futures.TimeoutError
            # aliases the built-in TimeoutError, so result(timeout=...) also
            # raises it for a coroutine's own timeout.
            # Resolve a done future without a timeout to propagate its stored
            # outcome, including completion racing with this polling timeout.
            if future.done():
                try:
                    return future.result()
                finally:
                    _close_if_scheduler_abandoned()
            continue
        except BaseException:
            _close_if_scheduler_abandoned()
            raise
        else:
            _close_if_scheduler_abandoned()
            return result


def _callable_identity(value: Any) -> tuple[int, int]:
    owner = getattr(value, "__self__", None)
    function = getattr(value, "__func__", None)
    if owner is not None and function is not None:
        return id(owner), id(function)
    return 0, id(value)


@dataclass(frozen=True)
class BoundReadOnlyMCPTool:
    """Capability bound to one live MCP server instance and profile.

    It bypasses the generic MCP handler because that path may reconnect,
    retry, or run OAuth recovery after its per-call timeout. Recall needs one
    cancellable call whose complete lifetime fits the caller's deadline.
    """

    server_name: str
    tool_name: str
    allowed_tools: frozenset[str]
    allowed_argument_keys: frozenset[str]
    profile_home: str
    registry_scope: str | None
    server_key: Any
    config_digest: str
    max_timeout: float
    max_response_chars: int
    raw_tool_names: tuple[str, ...]
    registered_tool_names: tuple[str, ...]
    tool_attestation: tuple[tuple[Any, ...], ...]
    initialize_attestation: tuple[int, str]
    registry_attestation: tuple[tuple[Any, ...], ...]
    session_call_attestation: tuple[int, int]
    _server: Any
    _session: Any
    _session_call: Any
    _rpc_lock: Any
    _shutdown_event: Any
    _reconnect_event: Any

    def _validate_live_server_binding(self) -> Any:
        with _lock:
            if _resolve_server_key(self.server_name, self.registry_scope, current=False) != self.server_key:
                raise RuntimeError("Read-only MCP capability server scope changed")
            server = _core._servers.get(self.server_key)
            if server is not self._server:
                raise RuntimeError("Read-only MCP capability live server instance changed")
            if server._rpc_lock is not self._rpc_lock:
                raise RuntimeError("Read-only MCP capability RPC lock changed")
            if server._shutdown_event is not self._shutdown_event:
                raise RuntimeError("Read-only MCP capability shutdown signal changed")
            if server._reconnect_event is not self._reconnect_event:
                raise RuntimeError("Read-only MCP capability reconnect signal changed")
            raw_names, registered_names = _validate_bound_server_instance(
                server,
                server_name=self.server_name,
                tool_name=self.tool_name,
                allowed_tools=self.allowed_tools,
                profile_home=self.profile_home,
                registry_scope=self.registry_scope,
                server_key=self.server_key,
                config_digest=self.config_digest,
                max_timeout=self.max_timeout,
            )
            if (
                raw_names != self.raw_tool_names
                or registered_names != self.registered_tool_names
                or _read_only_tool_attestation(server) != self.tool_attestation
                or _read_only_initialize_attestation(server) != self.initialize_attestation
                or _read_only_registry_attestation(registered_names, self.registry_scope)
                != self.registry_attestation
            ):
                raise RuntimeError("Read-only MCP capability tool provenance changed")
            session = server.session
            if session is not self._session:
                raise RuntimeError("Read-only MCP capability bound session changed")
            current_session_call = getattr(session, "call_tool", None)
            if (
                not callable(current_session_call)
                or _callable_identity(current_session_call)
                != self.session_call_attestation
            ):
                raise RuntimeError(
                    "Read-only MCP capability session call provenance changed"
                )
        if session is None:
            raise RuntimeError("Read-only MCP capability server is disconnected")
        return session

    def _validate_live_binding(self) -> Any:
        from hermes_constants import get_hermes_home

        if _normalize_profile_home(get_hermes_home()) != self.profile_home:
            raise RuntimeError("Read-only MCP capability profile context mismatch")
        if _core._mcp_registry_scope() != self.registry_scope:
            raise RuntimeError("Read-only MCP capability registry scope mismatch")
        if not _READ_ONLY_CONFIG_VALIDATION_LOCK.acquire(blocking=False):
            raise RuntimeError("Read-only MCP capability configuration check is busy")
        try:
            config = _load_raw_mcp_server_config(
                self.server_name, profile_home=self.profile_home
            )
        finally:
            _READ_ONLY_CONFIG_VALIDATION_LOCK.release()
        if not _read_only_binding_config_is_safe(
            config,
            tool_name=self.tool_name,
            allowed_tools=self.allowed_tools,
            max_timeout=self.max_timeout,
        ):
            raise RuntimeError("Read-only MCP capability configuration mismatch")
        if _mcp_config_digest(config) != self.config_digest:
            raise RuntimeError("Read-only MCP capability configuration changed")

        return self._validate_live_server_binding()

    def call(self, args: Dict[str, Any], *, deadline: float) -> Dict[str, Any]:
        if isinstance(deadline, bool):
            raise RuntimeError("Read-only MCP capability deadline is invalid")
        try:
            deadline_value = float(deadline)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Read-only MCP capability deadline is invalid") from exc
        if not math.isfinite(deadline_value):
            raise RuntimeError("Read-only MCP capability deadline is invalid")
        if deadline_value <= time.monotonic():
            raise TimeoutError("Read-only MCP capability deadline expired")
        if type(args) is not dict or not set(args) <= self.allowed_argument_keys:
            raise RuntimeError("Read-only MCP capability arguments are not allowed")
        frozen_args = _bounded_read_only_json(args, [1024, 8192])
        try:
            encoded_args = json.dumps(
                frozen_args,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Read-only MCP capability arguments are invalid") from exc
        if len(encoded_args) > 8192:
            raise RuntimeError("Read-only MCP capability arguments exceed the limit")

        remaining = min(self.max_timeout, deadline_value - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("Read-only MCP capability deadline expired")
        rpc_started = [False]

        async def _call_exact_session() -> Dict[str, Any]:
            call_timeout = min(
                self.max_timeout, deadline_value - time.monotonic()
            )
            if call_timeout <= 0:
                raise TimeoutError("Read-only MCP capability deadline expired")
            async with asyncio.timeout(call_timeout):
                async with self._rpc_lock:
                    session = await asyncio.to_thread(self._validate_live_binding)
                    if time.monotonic() >= deadline_value:
                        raise TimeoutError("Read-only MCP capability deadline expired")
                    rpc_started[0] = True
                    result = await self._session_call(
                        self.tool_name, arguments=frozen_args
                    )
                    post_session = await asyncio.to_thread(
                        self._validate_live_binding
                    )
                    if post_session is not session:
                        raise RuntimeError(
                            "Read-only MCP capability provenance changed during RPC"
                        )
                    if time.monotonic() >= deadline_value:
                        raise TimeoutError("Read-only MCP capability deadline expired")
                    serialized = _serialize_read_only_result(
                        result, self.max_response_chars
                    )
                    final_session = self._validate_live_server_binding()
                    if final_session is not session:
                        raise RuntimeError(
                            "Read-only MCP capability provenance changed during RPC"
                        )
                    if time.monotonic() >= deadline_value:
                        raise TimeoutError("Read-only MCP capability deadline expired")
                    return serialized

        async def _call_with_shutdown() -> Dict[str, Any]:
            rpc_task = asyncio.create_task(_call_exact_session())

            def _consume_result(done: asyncio.Task) -> None:
                if done.cancelled():
                    return
                try:
                    done.exception()
                except (asyncio.CancelledError, Exception):
                    pass

            rpc_task.add_done_callback(_consume_result)
            try:
                return await asyncio.shield(rpc_task)
            except (asyncio.CancelledError, TimeoutError):
                if rpc_started[0]:
                    self._reconnect_event.set()
                rpc_task.cancel()
                raise

        return _run_on_mcp_loop(_call_with_shutdown(), timeout=remaining)


def _normalize_profile_home(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return os.path.realpath(os.path.expanduser(raw))


_RAW_MCP_CONFIG_MAX_CHARS = 1_048_576
_STRICT_URL_DECODE_ROUNDS = 5
_READ_ONLY_CONFIG_VALIDATION_LOCK = threading.Lock()
_RAW_YAML_KEY_LINE_PATTERN = re.compile(
    r"^(?P<indent> *)(?P<key>[A-Za-z0-9_.-]+):(?P<rest>.*)$"
)


def _empty_yaml_mapping_value(rest: str) -> bool:
    stripped = rest.strip()
    return not stripped or stripped.startswith("#")


def _raw_yaml_document_is_unambiguous(raw_text: str) -> bool:
    import yaml
    from yaml.nodes import MappingNode, ScalarNode, SequenceNode

    try:
        root = yaml.compose(raw_text, Loader=yaml.BaseLoader)
    except yaml.YAMLError:
        return False
    if root is None:
        return False
    visiting: set[int] = set()
    budget = [10_000]

    def _walk(node: Any, depth: int = 0) -> bool:
        if depth > 64 or budget[0] <= 0 or id(node) in visiting:
            return False
        budget[0] -= 1
        visiting.add(id(node))
        try:
            if isinstance(node, MappingNode):
                seen = set()
                for key_node, value_node in node.value:
                    if not isinstance(key_node, ScalarNode) or key_node.value in seen:
                        return False
                    seen.add(key_node.value)
                    if not _walk(value_node, depth + 1):
                        return False
            elif isinstance(node, SequenceNode):
                if not all(_walk(item, depth + 1) for item in node.value):
                    return False
            elif not isinstance(node, ScalarNode):
                return False
            return True
        finally:
            visiting.remove(id(node))

    return _walk(root)


def _load_raw_mcp_server_config(
    server_name: str, *, profile_home: str = ""
) -> Dict[str, Any] | None:
    """Load one raw MCP server subtree without dotenv or config interpolation."""
    if not isinstance(server_name, str) or not server_name:
        return None
    import yaml

    target_lines: List[str] = []
    server_names: List[str] = []
    root_seen = False
    in_servers = False
    server_indent: int | None = None
    capturing_target = False
    target_count = 0
    total_chars = 0
    document_lines: List[str] = []

    try:
        from hermes_constants import get_config_path, get_hermes_home

        active_home = _normalize_profile_home(get_hermes_home())
        expected_home = _normalize_profile_home(profile_home)
        if expected_home and active_home != expected_home:
            return None
        config_path = get_config_path()
        with config_path.open(encoding="utf-8") as handle:
            for line in handle:
                total_chars += len(line)
                if total_chars > _RAW_MCP_CONFIG_MAX_CHARS:
                    return None
                document_lines.append(line)

                leading = line[: len(line) - len(line.lstrip(" \t"))]
                if "\t" in leading:
                    return None
                stripped = line.strip()
                match = _RAW_YAML_KEY_LINE_PATTERN.match(line.rstrip("\r\n"))

                if (
                    match is not None
                    and not match.group("indent")
                    and match.group("key") == "mcp_servers"
                ):
                    if root_seen or not _empty_yaml_mapping_value(
                        match.group("rest")
                    ):
                        return None
                    root_seen = True
                    in_servers = True
                    server_indent = None
                    capturing_target = False
                    continue

                if not in_servers:
                    continue
                if not stripped:
                    if capturing_target:
                        target_lines.append("\n")
                    continue
                if stripped.startswith("#"):
                    if capturing_target and server_indent is not None:
                        indent = len(line) - len(line.lstrip(" "))
                        if indent >= server_indent:
                            target_lines.append(line)
                    continue

                indent = len(line) - len(line.lstrip(" "))
                if indent == 0:
                    in_servers = False
                    capturing_target = False
                    continue
                if server_indent is None:
                    if match is None:
                        return None
                    server_indent = indent
                if indent < server_indent:
                    return None
                if indent == server_indent:
                    if match is None or len(match.group("indent")) != server_indent:
                        return None
                    name = match.group("key")
                    if name in server_names:
                        return None
                    server_names.append(name)
                    capturing_target = False
                    if name == server_name:
                        target_count += 1
                        if target_count != 1 or not _empty_yaml_mapping_value(
                            match.group("rest")
                        ):
                            return None
                        target_lines = [line]
                        capturing_target = True
                    continue
                if capturing_target:
                    target_lines.append(line)

        if not _raw_yaml_document_is_unambiguous("".join(document_lines)):
            return None
        if not root_seen or target_count != 1 or server_indent is None:
            return None
        canonical_name = sanitize_mcp_name_component(server_name)
        if any(
            name != server_name
            and sanitize_mcp_name_component(name) == canonical_name
            for name in server_names
        ):
            return None

        dedented_target = []
        prefix = " " * server_indent
        for line in target_lines:
            if line.strip():
                if not line.startswith(prefix):
                    return None
                dedented_target.append(line[server_indent:])
            else:
                dedented_target.append(line)
        payload = yaml.safe_load("".join(dedented_target))
    except (OSError, TypeError, ValueError, yaml.YAMLError):
        return None

    if not isinstance(payload, dict):
        return None
    config = payload.get(server_name)
    return copy.deepcopy(config) if isinstance(config, dict) else None


def _strict_loopback_mcp_url_is_safe(
    value: Any, *, require_ip_literal: bool = False
) -> bool:
    """Validate a canonical loopback HTTP URL after bounded normalization."""
    try:
        raw_url = str(value)
        if (
            not raw_url
            or raw_url != raw_url.strip()
            or unicodedata.normalize("NFKC", raw_url) != raw_url
            or "?" in raw_url
            or "#" in raw_url
            or "%" in raw_url
            or ";" in raw_url
            or any(
                char == "\\" or char.isspace() or ord(char) < 32 or ord(char) == 127
                for char in raw_url
            )
        ):
            return False
        parsed = urlparse(raw_url)
        if parsed.scheme not in {"http", "https"}:
            return False
        if (
            parsed.username is not None
            or parsed.password is not None
            or not parsed.hostname
            or parsed.params
            or parsed.query
            or parsed.fragment
            or "%" in parsed.netloc
        ):
            return False
        port = parsed.port
        if parsed.hostname.lower() == "localhost":
            if require_ip_literal:
                return False
            canonical_host = "localhost"
        else:
            try:
                endpoint_ip = ipaddress.ip_address(parsed.hostname)
            except ValueError:
                return False
            if not endpoint_ip.is_loopback:
                return False
            canonical_host = (
                f"[{endpoint_ip.compressed}]"
                if endpoint_ip.version == 6
                else endpoint_ip.compressed
            )

        path = parsed.path
        for _round in range(_STRICT_URL_DECODE_ROUNDS):
            normalized = unicodedata.normalize("NFKC", path)
            if normalized != path or any(
                char in ";\\"
                or char.isspace()
                or ord(char) < 32
                or ord(char) == 127
                for char in normalized
            ):
                return False
            decoded = unquote(normalized, errors="strict")
            if decoded == normalized:
                path = decoded
                break
            path = decoded
        else:
            return False
        if "%" in path or any(segment in {".", ".."} for segment in path.split("/")):
            return False
        canonical_netloc = canonical_host if port is None else f"{canonical_host}:{port}"
        canonical_url = f"{parsed.scheme}://{canonical_netloc}{path}"
        return canonical_url == raw_url
    except (TypeError, ValueError, UnicodeError):
        return False


def _mcp_config_digest(config: Any) -> str:
    if not isinstance(config, dict):
        return ""
    try:
        encoded = json.dumps(
            config,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


_READ_ONLY_BINDING_CONFIG_KEYS = frozenset({
    "elicitation",
    "enabled",
    "follow_redirects",
    "model_visible",
    "sampling",
    "timeout",
    "tools",
    "transport",
    "url",
})
_READ_ONLY_BINDING_TOOL_CONFIG_KEYS = frozenset({
    "exclude",
    "include",
    "prompts",
    "resources",
})


def _read_only_binding_config_is_safe(
    config: Any,
    *,
    tool_name: str,
    allowed_tools: frozenset[str],
    max_timeout: float,
) -> bool:
    if (
        not isinstance(config, dict)
        or set(config) - _READ_ONLY_BINDING_CONFIG_KEYS
        or config.get("enabled") is not True
        or config.get("follow_redirects") is not False
        or config.get("model_visible") is not False
    ):
        return False
    transport = str(config.get("transport") or "streamable_http").strip().lower()
    if transport not in {"http", "streamable-http", "streamable_http"}:
        return False
    if not _strict_loopback_mcp_url_is_safe(
        config.get("url"), require_ip_literal=True
    ):
        return False
    for capability_name in ("sampling", "elicitation"):
        if config.get(capability_name) != {"enabled": False}:
            return False
    tools = config.get("tools")
    if (
        not isinstance(tools, dict)
        or set(tools) - _READ_ONLY_BINDING_TOOL_CONFIG_KEYS
        or not {"include", "resources", "prompts"} <= set(tools)
    ):
        return False
    if tools.get("resources") is not False or tools.get("prompts") is not False:
        return False
    if tools.get("exclude") not in (None, []):
        return False
    include = tools.get("include")
    if not isinstance(include, list) or not include:
        return False
    included = [str(name).strip() for name in include]
    if any(not name for name in included) or len(included) != len(set(included)):
        return False
    included_set = set(included)
    if tool_name not in included_set or included_set != allowed_tools:
        return False
    raw_timeout = config.get("timeout")
    if isinstance(raw_timeout, bool):
        return False
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError):
        return False
    return math.isfinite(timeout) and 0 < timeout <= max_timeout


def _attestation_digest(value: Any) -> str:
    if value is not None and callable(getattr(value, "model_dump", None)):
        value = value.model_dump(mode="json")
    elif type(value).__name__ == "SimpleNamespace" and hasattr(value, "__dict__"):
        value = dict(vars(value))
    bounded = _bounded_read_only_json(value, [4096, 65_536])
    encoded = json.dumps(
        bounded, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _read_only_tool_attestation(server: Any) -> tuple[tuple[Any, ...], ...]:
    records = []
    for tool in getattr(server, "_tools", ()):  # validated by caller
        schema = {
            "name": getattr(tool, "name", None),
            "description": getattr(tool, "description", None),
            "inputSchema": getattr(tool, "inputSchema", None),
            "outputSchema": getattr(tool, "outputSchema", None),
        }
        records.append((id(tool), schema["name"], _attestation_digest(schema)))
    return tuple(records)


def _read_only_initialize_attestation(server: Any) -> tuple[int, str]:
    result = getattr(server, "initialize_result", None)
    if result is None:
        raise RuntimeError("Read-only MCP capability initialization provenance missing")
    return id(result), _attestation_digest(result)


def _read_only_registry_attestation(
    names: tuple[str, ...], scope: str | None = None,
) -> tuple[tuple[Any, ...], ...]:
    from tools.registry import registry

    records = []
    for name in names:
        entry = registry.snapshot_registration(name, scope=scope)
        if entry is None:
            raise RuntimeError("Read-only MCP capability registry provenance missing")
        if getattr(entry, "expose_to_model", True):
            raise RuntimeError("Read-only MCP capability is exposed to the model")
        records.append(
            (
                name,
                id(entry),
                id(entry.handler),
                id(entry.check_fn),
                entry.toolset,
                bool(entry.is_async),
                bool(entry.expose_to_model),
                _attestation_digest(entry.schema),
            )
        )
    return tuple(records)


def _validate_bound_server_instance(
    server: Any,
    *,
    server_name: str,
    tool_name: str,
    allowed_tools: frozenset[str],
    profile_home: str,
    registry_scope: str | None,
    server_key: Any,
    config_digest: str,
    max_timeout: float,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if getattr(server, "name", None) != server_name:
        raise RuntimeError("Read-only MCP capability server provenance mismatch")
    if registry_scope is not None:
        from hermes_constants import hermes_home_key
        if registry_scope != hermes_home_key(profile_home):
            raise RuntimeError("Read-only MCP capability registry profile mismatch")
    if (server_key not in _core._server_scope_keys
            or _core._server_scope_keys[server_key] != _key_scope(server_key)):
        raise RuntimeError("Read-only MCP capability server owner mismatch")
    if registry_scope is not None and not _core._server_visible_in_scope(server_key, registry_scope):
        raise RuntimeError("Read-only MCP capability server profile mismatch")
    if _mcp_config_digest(getattr(server, "_config", None)) != config_digest:
        raise RuntimeError("Read-only MCP capability server configuration mismatch")
    raw_timeout = getattr(server, "tool_timeout", None)
    if isinstance(raw_timeout, bool):
        raise RuntimeError("Read-only MCP capability server timeout mismatch")
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Read-only MCP capability server timeout mismatch") from exc
    if not math.isfinite(timeout) or not 0 < timeout <= max_timeout:
        raise RuntimeError("Read-only MCP capability server timeout mismatch")
    if getattr(server, "_sampling", None) is not None or getattr(
        server, "_elicitation", None
    ) is not None:
        raise RuntimeError("Read-only MCP capability interactive handler mismatch")
    tools = getattr(server, "_tools", None)
    if not isinstance(tools, list):
        raise RuntimeError("Read-only MCP capability tool provenance mismatch")
    tool_names = [getattr(tool, "name", None) for tool in tools]
    if (
        any(not isinstance(name, str) or not name for name in tool_names)
        or len(tool_names) != len(set(tool_names))
        or tool_names.count(tool_name) != 1
    ):
        raise RuntimeError("Read-only MCP capability tool provenance mismatch")
    registered_names = getattr(server, "_registered_tool_names", None)
    if not isinstance(registered_names, list) or any(
        not isinstance(name, str) or not name for name in registered_names
    ):
        raise RuntimeError("Read-only MCP capability tool provenance mismatch")
    expected_registered_names = {
        mcp_prefixed_tool_name(server_name, name) for name in allowed_tools
    }
    if (
        len(registered_names) != len(set(registered_names))
        or set(registered_names) != expected_registered_names
    ):
        raise RuntimeError("Read-only MCP capability tool provenance mismatch")
    if getattr(server, "session", None) is None:
        raise RuntimeError("Read-only MCP capability server is disconnected")
    return tuple(sorted(tool_names)), tuple(sorted(registered_names))


def _bounded_read_only_json(value: Any, budget: list[int], depth: int = 0) -> Any:
    if depth > 8 or budget[0] <= 0:
        raise RuntimeError("Read-only MCP response exceeds the limit")
    budget[0] -= 1
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if not -(2**63) <= value < 2**63:
            raise RuntimeError("Read-only MCP response is malformed")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise RuntimeError("Read-only MCP response is malformed")
        return value
    if type(value) is str:
        budget[1] -= len(value)
        if budget[1] < 0:
            raise RuntimeError("Read-only MCP response exceeds the limit")
        return value
    if type(value) is list:
        if len(value) > 256:
            raise RuntimeError("Read-only MCP response exceeds the limit")
        return [
            _bounded_read_only_json(item, budget, depth + 1) for item in value
        ]
    if type(value) is dict:
        if len(value) > 256:
            raise RuntimeError("Read-only MCP response exceeds the limit")
        bounded = {}
        for key, item in value.items():
            if type(key) is not str:
                raise RuntimeError("Read-only MCP response is malformed")
            budget[1] -= len(key)
            if budget[1] < 0:
                raise RuntimeError("Read-only MCP response exceeds the limit")
            bounded[key] = _bounded_read_only_json(item, budget, depth + 1)
        return bounded
    raise RuntimeError("Read-only MCP response is malformed")


def _serialize_read_only_result(result: Any, max_response_chars: int) -> Dict[str, Any]:
    if getattr(result, "isError", False):
        raise RuntimeError("Read-only MCP tool returned an error")
    payload: Dict[str, Any] = {}
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        payload["structuredContent"] = _bounded_read_only_json(
            structured, [4096, max_response_chars]
        )
    content = getattr(result, "content", None)
    if content is not None and type(content) is not list:
        raise RuntimeError("Read-only MCP response is malformed")
    if type(content) is list:
        text_parts = []
        text_chars = 0
        for block in content[:64]:
            text = getattr(block, "text", None)
            if not isinstance(text, str):
                continue
            text_chars += len(text)
            if text_chars > max_response_chars:
                raise RuntimeError("Read-only MCP response exceeds the limit")
            text_parts.append(text)
        if text_parts:
            payload["result"] = "\n".join(text_parts)
    try:
        encoded = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Read-only MCP response is malformed") from exc
    if len(encoded) > max_response_chars:
        raise RuntimeError("Read-only MCP response exceeds the limit")
    return payload


def bind_read_only_mcp_tool(
    *,
    server_name: str,
    tool_name: str,
    allowed_tools: frozenset[str],
    allowed_argument_keys: frozenset[str],
    profile_home: str,
    max_timeout: float,
    max_response_chars: int,
) -> BoundReadOnlyMCPTool:
    """Bind one read-only call to the exact current server, config, and profile."""
    if tool_name not in allowed_tools or not allowed_argument_keys:
        raise RuntimeError("Read-only MCP capability declaration is invalid")
    normalized_home = _normalize_profile_home(profile_home)
    from hermes_constants import get_hermes_home

    if _normalize_profile_home(get_hermes_home()) != normalized_home:
        raise RuntimeError("Read-only MCP capability profile context mismatch")
    if not _READ_ONLY_CONFIG_VALIDATION_LOCK.acquire(blocking=False):
        raise RuntimeError("Read-only MCP capability configuration validation is busy")
    try:
        config = _load_raw_mcp_server_config(
            server_name, profile_home=normalized_home
        )
    finally:
        _READ_ONLY_CONFIG_VALIDATION_LOCK.release()
    if not _read_only_binding_config_is_safe(
        config,
        tool_name=tool_name,
        allowed_tools=allowed_tools,
        max_timeout=max_timeout,
    ):
        raise RuntimeError("Read-only MCP capability configuration mismatch")
    digest = _mcp_config_digest(config)
    if not digest:
        raise RuntimeError("Read-only MCP capability configuration is invalid")
    bound_timeout = min(float(max_timeout), float(config["timeout"]))
    registry_scope = _core._mcp_registry_scope()
    with _lock:
        server_key = _resolve_server_key(server_name, registry_scope, current=False)
        server = _core._servers.get(server_key)
        if server is None:
            raise RuntimeError("Read-only MCP capability live server is unavailable")
        raw_tool_names, registered_tool_names = _validate_bound_server_instance(
            server,
            server_name=server_name,
            tool_name=tool_name,
            allowed_tools=allowed_tools,
            profile_home=normalized_home,
            registry_scope=registry_scope,
            server_key=server_key,
            config_digest=digest,
            max_timeout=bound_timeout,
        )
        session = server.session
        if session is None:
            raise RuntimeError("Read-only MCP capability server is disconnected")
        session_call = getattr(session, "call_tool", None)
        if not callable(session_call):
            raise RuntimeError("Read-only MCP capability session call is unavailable")
        session_call_attestation = _callable_identity(session_call)
        tool_attestation = _read_only_tool_attestation(server)
        initialize_attestation = _read_only_initialize_attestation(server)
        registry_attestation = _read_only_registry_attestation(registered_tool_names, registry_scope)
        rpc_lock = server._rpc_lock
        shutdown_event = server._shutdown_event
        reconnect_event = server._reconnect_event
    return BoundReadOnlyMCPTool(
        server_name=server_name,
        tool_name=tool_name,
        allowed_tools=frozenset(allowed_tools),
        allowed_argument_keys=frozenset(allowed_argument_keys),
        profile_home=normalized_home,
        registry_scope=registry_scope,
        server_key=server_key,
        config_digest=digest,
        max_timeout=bound_timeout,
        max_response_chars=int(max_response_chars),
        raw_tool_names=raw_tool_names,
        registered_tool_names=registered_tool_names,
        tool_attestation=tool_attestation,
        initialize_attestation=initialize_attestation,
        registry_attestation=registry_attestation,
        session_call_attestation=session_call_attestation,
        _server=server,
        _session=session,
        _session_call=session_call,
        _rpc_lock=rpc_lock,
        _shutdown_event=shutdown_event,
        _reconnect_event=reconnect_event,
    )
