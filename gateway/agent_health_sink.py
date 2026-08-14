"""Non-blocking Discord sink for gateway health alerts."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from typing import Any, Optional

from gateway.agent_health import (
    AlertBudget,
    HealthEvent,
    UpstreamFailureTracker,
    classify_log_record,
    format_health_alert,
)


_SEND_TIMEOUT_SECONDS = 15.0
_ACTIVE_SINK: Optional["AgentHealthSink"] = None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    token = raw.strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def set_active_agent_health_sink(sink: Optional["AgentHealthSink"]) -> None:
    global _ACTIVE_SINK
    _ACTIVE_SINK = sink


def get_active_agent_health_sink() -> Optional["AgentHealthSink"]:
    return _ACTIVE_SINK


class AgentHealthLogHandler(logging.Handler):
    """Synchronous allowlist-only bridge from logging into the async sink.

    The handler performs no logging and no blocking I/O.  It is intentionally
    safe on model worker threads and cannot recurse through the root logger.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self._hermes_agent_health = True

    def emit(self, record: logging.LogRecord) -> None:
        try:
            event = classify_log_record(record.name, record.getMessage())
            if event is None:
                return
            session_tag = str(getattr(record, "session_tag", "") or "").strip()
            session_id = ""
            if session_tag.startswith("[") and session_tag.endswith("]"):
                session_id = session_tag[1:-1].strip()
            event = dataclasses.replace(
                event,
                session_id=session_id,
                occurred_at=float(getattr(record, "created", event.occurred_at)),
            )
            sink = get_active_agent_health_sink()
            if sink is not None:
                sink.emit(event)
        except Exception:
            return


def install_agent_health_log_handler(root: logging.Logger) -> None:
    """Install exactly one health handler on ``root``."""
    for handler in root.handlers:
        if getattr(handler, "_hermes_agent_health", False):
            return
    root.addHandler(AgentHealthLogHandler())


class AgentHealthSink:
    """Single-task, bounded-queue alert delivery runtime."""

    def __init__(
        self,
        gateway: Any,
        *,
        enabled: bool,
        channel: str,
        mention: str,
        cooldown_seconds: float = 900,
        hourly_cap: int = 12,
        upstream_failure_streak: int = 3,
        queue_size: int = 256,
    ) -> None:
        self.gateway = gateway
        self.enabled = bool(enabled and str(channel).strip())
        self.channel = str(channel or "").strip()
        self.mention = str(mention or "").strip()
        self.queue: asyncio.Queue[HealthEvent] = asyncio.Queue(
            maxsize=max(1, int(queue_size))
        )
        self.budget = AlertBudget(cooldown_seconds, hourly_cap)
        self.upstream = UpstreamFailureTracker(
            threshold=upstream_failure_streak,
            window_seconds=600,
        )
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None
        self._closed = False
        self._dropped = 0

    @classmethod
    def from_environment(cls, gateway: Any) -> "AgentHealthSink":
        return cls(
            gateway,
            enabled=_env_bool("HERMES_HEALTH_ENABLED", False),
            channel=os.getenv("HERMES_HEALTH_CHANNEL", ""),
            mention=os.getenv("HERMES_HEALTH_MENTION", ""),
            cooldown_seconds=_env_float("HERMES_HEALTH_COOLDOWN_SECONDS", 900),
            hourly_cap=_env_int("HERMES_HEALTH_HOURLY_CAP", 12),
            upstream_failure_streak=_env_int(
                "HERMES_HEALTH_UPSTREAM_FAILURE_STREAK", 3
            ),
        )

    def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        try:
            self._loop = asyncio.get_running_loop()
            self._task = self._loop.create_task(self._drain())
            set_active_agent_health_sink(self)
        except Exception:
            self._loop = None
            self._task = None

    def emit(self, event: HealthEvent) -> bool:
        """Schedule ``event`` without waiting for queue space or network I/O."""
        if not self.enabled or self._closed or self._loop is None:
            return False
        try:
            self._loop.call_soon_threadsafe(self._put_nowait, event)
            return True
        except Exception:
            return False

    def _put_nowait(self, event: HealthEvent) -> None:
        if self._closed:
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped += 1
        except Exception:
            self._dropped += 1

    def _discord_adapter(self) -> Any:
        try:
            adapters = self.gateway._iter_gateway_adapters()
        except Exception:
            return None
        try:
            for adapter in adapters:
                platform = getattr(adapter, "platform", None)
                value = getattr(platform, "value", platform)
                if str(value or "").lower() == "discord":
                    return adapter
        except Exception:
            return None
        return None

    async def _wait_for_discord_adapter(self) -> Any:
        while not self._closed:
            adapter = self._discord_adapter()
            if adapter is not None and getattr(adapter, "is_connected", True):
                return adapter
            await asyncio.sleep(1)
        return None

    def _expand_event(self, event: HealthEvent) -> list[HealthEvent]:
        """Apply C3 aggregation while retaining B's terminal-error alert."""
        outputs: list[HealthEvent] = []
        is_upstream_sample = event.rule in {
            "C3.stream_stale",
            "B.api_retries_exhausted",
        }
        if event.rule != "C3.stream_stale":
            outputs.append(event)
        if is_upstream_sample:
            aggregate = self.upstream.record(event, now=event.occurred_at)
            if aggregate is not None:
                outputs.append(aggregate)
        return outputs

    @staticmethod
    def _redact_event(event: HealthEvent) -> HealthEvent:
        """Force-redact every operator-controlled string before Discord egress."""
        try:
            from agent.redact import redact_sensitive_text

            def redact(value: str) -> str:
                return redact_sensitive_text(str(value or ""), force=True)

            return dataclasses.replace(
                event,
                title=redact(event.title),
                reason=redact(event.reason),
                action=redact(event.action),
                session_id=redact(event.session_id),
                session_key=redact(event.session_key),
                platform=redact(event.platform),
                resource=redact(event.resource),
                jump_url=redact(event.jump_url),
                details=tuple(redact(detail) for detail in event.details),
            )
        except Exception:
            # A redaction failure must fail closed at this external boundary:
            # the structured failover trace and its counters are event-derived
            # too, so they are cleared alongside the free-text fields.
            return dataclasses.replace(
                event,
                title="에이전트 헬스 이벤트",
                reason="상세 사유를 안전하게 마스킹하지 못했습니다.",
                action="서버의 redacted 로그를 확인하세요.",
                session_id="",
                session_key="",
                platform="",
                resource="",
                jump_url="",
                first_endpoint="",
                first_reason="",
                route_from="",
                route_to="",
                last_endpoint="",
                last_reason="",
                retry_count=None,
                message_count=None,
                token_estimate=None,
                details=(),
            )

    async def _send_one(self, event: HealthEvent) -> None:
        adapter = await self._wait_for_discord_adapter()
        if adapter is None or self._closed:
            return
        admitted = self.budget.admit(event)
        if admitted is None:
            return
        if self._dropped:
            admitted = dataclasses.replace(
                admitted,
                suppressed_count=admitted.suppressed_count + self._dropped,
            )
            self._dropped = 0
        admitted = self._redact_event(admitted)
        text = format_health_alert(admitted, mention_text=self.mention)
        try:
            result = await asyncio.wait_for(
                adapter.send(
                    self.channel,
                    text,
                    metadata={"notify": True, "agent_health": True},
                ),
                timeout=_SEND_TIMEOUT_SECONDS,
            )
            if result is not None and not getattr(result, "success", True):
                return
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return
        except Exception:
            return

    async def _drain(self) -> None:
        while not self._closed:
            try:
                event = await self.queue.get()
            except asyncio.CancelledError:
                return
            try:
                for output in self._expand_event(event):
                    await self._send_one(output)
            except asyncio.CancelledError:
                return
            except Exception:
                pass
            finally:
                try:
                    self.queue.task_done()
                except Exception:
                    pass

    async def stop(self) -> None:
        self._closed = True
        if get_active_agent_health_sink() is self:
            set_active_agent_health_sink(None)
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
