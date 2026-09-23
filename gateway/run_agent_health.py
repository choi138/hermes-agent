"""Agent-health policy and evidence for a running gateway.

Delivery, startup, and shutdown remain owned by their existing gateway mixins.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("gateway.run")


class GatewayAgentHealthMixin:
    def _record_platform_health_transition(
        self, platform: str, *, platform_state: Optional[str],
        error_code: Optional[str], error_message: Optional[str],
    ) -> None:
        """Emit C2 only on meaningful degraded/recovered state transitions."""
        if platform_state is None:
            return
        normalized = {
            "retrying": "degraded", "degraded": "degraded",
            "paused": "parked", "parked": "parked",
            "fatal": "failed", "failed": "failed",
            "connected": "connected",
        }.get(str(platform_state).strip().lower(), "transitional")
        states = getattr(self, "_agent_health_platform_states", None)
        if not isinstance(states, dict):
            states = self._agent_health_platform_states = {}
        previous = states.get(platform)
        if normalized == "transitional":
            if previous is None:
                states[platform] = normalized
            return
        if normalized == previous:
            return
        states[platform] = normalized
        unhealthy = {"degraded", "parked", "failed"}

        from gateway.agent_health import HealthEvent

        if normalized in unhealthy:
            detail = str(error_message or error_code or "상세 원인 없음")
            event = HealthEvent(
                rule=f"C2.platform_{normalized}", category="C",
                title=f"플랫폼 어댑터 {normalized}",
                reason=f"{platform} 어댑터가 {previous or 'unknown'}에서 {normalized} 상태로 전이했습니다.",
                action="게이트웨이 연결, 자격 증명, 플랫폼 상태를 확인하세요.",
                platform=platform, resource=platform, details=(detail[:1200],), mention=True,
            )
        elif normalized == "connected" and previous in unhealthy:
            event = HealthEvent(
                rule="C2.platform_recovered", category="C", title="플랫폼 어댑터 복구",
                reason=f"{platform} 어댑터가 {previous} 상태에서 connected로 복구했습니다.",
                action="누락된 인바운드가 재전달되는지 확인하세요.",
                platform=platform, resource=platform, mention=True,
            )
        else:
            return
        self._emit_agent_health_event(event)

    def _agent_health_silence_timeout_seconds(self) -> float:
        from gateway.run import _float_env

        return _float_env("HERMES_HEALTH_SILENCE_TIMEOUT", 600)

    def _agent_health_turn_deadline_seconds(self) -> float:
        from gateway.run import _float_env

        return _float_env("HERMES_HEALTH_TURN_DEADLINE", 1500)

    def _emit_agent_health_event(self, event) -> bool:
        """Best-effort non-blocking enqueue into the process health sink."""
        sink = getattr(self, "_agent_health_sink", None)
        if sink is None:
            try:
                from gateway.agent_health_sink import get_active_agent_health_sink

                sink = get_active_agent_health_sink()
            except Exception:
                sink = None
        if sink is None:
            return False
        try:
            return bool(sink.emit(event))
        except Exception:
            return False

    @staticmethod
    def _agent_health_last_matching_log_line(path: Path, marker: str) -> str:
        """Read a bounded file tail and return the newest matching line."""
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 131072), os.SEEK_SET)
                tail = handle.read().decode("utf-8", errors="replace")
        except OSError:
            return ""
        for line in reversed(tail.splitlines()):
            if marker in line:
                return line.strip()[:1600]
        return ""

    @staticmethod
    def _agent_health_previous_exit_diag_line(
        path: Path,
        *,
        previous_pid: Optional[int] = None,
        current_pid: Optional[int] = None,
    ) -> str:
        """Return the newest exit-diag record owned by the previous gateway.

        ``gateway.start`` is written before :func:`start_gateway` reaches the
        health bootstrap below, so blindly taking the file's last line reports
        the *new* process as the previous exit.  Prefer the PID persisted in
        the prior runtime status and otherwise exclude the current PID.
        """
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 131072), os.SEEK_SET)
                lines = handle.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            return ""

        fallback = ""
        for line in reversed(lines):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except (TypeError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            record_pid = record.get("pid")
            prior_pid = record.get("prior_pid")
            if previous_pid is not None and (
                record_pid == previous_pid or prior_pid == previous_pid
            ):
                return stripped[:1600]
            if (
                not fallback
                and current_pid is not None
                and record_pid != current_pid
            ):
                fallback = stripped[:1600]
        return fallback

    def _emit_gateway_restart_health(self) -> bool:
        """C1 startup summary with prior status, exit diag, and memory tail."""
        from gateway.agent_health import HealthEvent

        previous = getattr(self, "_agent_health_previous_status", None)
        previous = previous if isinstance(previous, dict) else {}
        lifecycle = getattr(self, "_agent_health_lifecycle_evidence", None)
        lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
        previous_heartbeat = getattr(
            self, "_agent_health_previous_heartbeat", None
        )
        previous_heartbeat = (
            previous_heartbeat if isinstance(previous_heartbeat, dict) else {}
        )
        prev_state = str(previous.get("gateway_state") or "unknown")
        exit_reason = str(previous.get("exit_reason") or "")
        memory = lifecycle.get("last_heartbeat_mem") or previous_heartbeat.get(
            "mem"
        )
        rss = memory.get("rss_kib") if isinstance(memory, dict) else None

        from gateway.run import _hermes_home

        log_dir = _hermes_home / "logs"
        exit_diag = getattr(self, "_agent_health_previous_exit_diag", None)
        if exit_diag is None:
            exit_diag = self._agent_health_previous_exit_diag_line(
                log_dir / "gateway-exit-diag.log",
                previous_pid=previous.get("pid"),
                current_pid=os.getpid(),
            )
        if not exit_reason and exit_diag:
            try:
                exit_reason = str(json.loads(exit_diag).get("tag") or "")
            except (TypeError, ValueError, AttributeError):
                pass
        exit_reason = exit_reason or "unknown"
        memory_line = getattr(
            self, "_agent_health_previous_memory_line", None
        )
        if memory_line is None:
            memory_line = self._agent_health_last_matching_log_line(
                log_dir / "gateway.log", "[MEMORY]"
            ) or self._agent_health_last_matching_log_line(
                log_dir / "agent.log", "[MEMORY]"
            ) or self._agent_health_last_matching_log_line(
                log_dir / "agent.log", "rss_kib="
            )
        rss_text = (
            f"{float(rss) / 1024:.1f} MiB"
            if isinstance(rss, (int, float))
            else "unknown"
        )
        if rss_text == "unknown" and memory_line:
            rss_match = re.search(r"\brss=(\d+(?:\.\d+)?)MB\b", memory_line)
            if rss_match:
                rss_text = f"{float(rss_match.group(1)):.1f} MiB"
            else:
                rss_kib_match = re.search(
                    r"\brss_kib=(\d+)(?:->(\d+))?\b", memory_line
                )
                if rss_kib_match:
                    rss_kib = rss_kib_match.group(2) or rss_kib_match.group(1)
                    rss_text = f"{float(rss_kib) / 1024:.1f} MiB"
        heartbeat_detail = ""
        if previous_heartbeat:
            heartbeat_detail = (
                "직전 heartbeat: "
                f"updated_at={previous_heartbeat.get('updated_at') or 'unknown'} "
                f"mem={previous_heartbeat.get('mem') or {}}"
            )
        details = tuple(
            item
            for item in (
                f"직전 runtime_status updated_at={previous.get('updated_at') or 'unknown'}",
                f"직전 exit diag: {exit_diag}" if exit_diag else "",
                heartbeat_detail,
                f"마지막 메모리 로그: {memory_line}" if memory_line else "",
            )
            if item
        )
        suspected_oom = bool(lifecycle.get("suspected_oom"))
        return self._emit_agent_health_event(
            HealthEvent(
                rule="C1.gateway_restart",
                category="C",
                title="Hermes 게이트웨이 재시작됨",
                reason=(
                    f"게이트웨이가 기동했습니다. 직전 state={prev_state}, "
                    f"exit_reason={exit_reason}, 직전 RSS={rss_text}."
                ),
                action=(
                    "OOM 가능성이 감지되었습니다. 메모리 압력과 커널 로그를 확인하세요."
                    if suspected_oom
                    else "비정상 종료 여부와 직전 진단 로그를 확인하세요."
                ),
                resource="hermes-gateway",
                details=details,
                mention=True,
            )
        )

    def _record_content_delivered(
        self,
        session_key: str,
        run_generation: Optional[int] = None,
    ) -> None:
        """Advance A's output clock after a confirmed platform ACK.

        Stale generations are ignored so a late send from an interrupted turn
        cannot mask a silent replacement turn.  Successful content re-arms the
        silence warning for a later silence episode in the same generation.
        """
        if not session_key:
            return
        if run_generation is not None and not self._is_session_run_current(
            session_key, run_generation
        ):
            return
        state = self._peek_session_state(session_key)
        generation = int(
            run_generation
            if run_generation is not None
            else (state.persistent.run_generation if state is not None else 0)
        )
        timestamps = getattr(self, "_last_content_sent_at", None)
        if timestamps is None:
            timestamps = {}
            self._last_content_sent_at = timestamps
        timestamps[session_key] = time.time()
        notified = getattr(self, "_output_silence_notified", None)
        if isinstance(notified, dict):
            notified.pop((session_key, generation), None)

        waiting = getattr(self, "_output_silence_user_waiting", None)
        if isinstance(waiting, dict):
            waiting.pop((session_key, generation), None)

    def _session_waiting_on_user(self, session_key: str) -> bool:
        """Return whether a live turn is blocked on an explicit user prompt.

        Only concrete gateway primitives count.  Agent activity, heartbeat
        touches, and free-form status text intentionally remain outside A's
        policy so busy retry loops cannot hide real output silence.
        """
        if not session_key:
            return False
        try:
            from tools import clarify_gateway as clarify_mod

            if clarify_mod.get_pending_for_session(
                session_key,
                include_choice_prompts=True,
            ) is not None:
                return True
        except Exception:
            logger.debug(
                "Could not inspect pending clarify for output-silence policy",
                exc_info=True,
            )
        try:
            from tools.approval import has_blocking_approval

            return bool(has_blocking_approval(session_key))
        except Exception:
            logger.debug(
                "Could not inspect pending approval for output-silence policy",
                exc_info=True,
            )
            return False

    def _resume_output_silence_clock(
        self,
        session_key: str,
        run_generation: int,
    ) -> bool:
        """Restart A's clock after an observed explicit user wait ends."""
        if not session_key or not self._is_session_run_current(
            session_key, run_generation
        ):
            return False
        timestamps = getattr(self, "_last_content_sent_at", None)
        if not isinstance(timestamps, dict):
            timestamps = {}
            self._last_content_sent_at = timestamps
        timestamps[session_key] = time.time()
        latch_key = (session_key, int(run_generation))
        for attr in (
            "_output_silence_notified",
            "_turn_deadline_enforced",
            "_output_silence_user_waiting",
        ):
            latches = getattr(self, attr, None)
            if isinstance(latches, dict):
                latches.pop(latch_key, None)
        return True

    def _agent_health_session_id(self, session_key: str) -> str:
        try:
            value = self.session_store.peek_session_id(session_key)
            return str(value or "")
        except Exception:
            return ""

    def _agent_health_event_context(self, session_key: str, source) -> dict:
        from gateway.agent_health import discord_jump_url

        platform = getattr(getattr(source, "platform", None), "value", "")
        jump_url = ""
        if platform == "discord":
            jump_url = discord_jump_url(
                guild_id=str(
                    getattr(source, "scope_id", None)
                    or getattr(source, "guild_id", None)
                    or ""
                ),
                chat_id=str(getattr(source, "chat_id", "") or ""),
                thread_id=str(getattr(source, "thread_id", "") or ""),
                message_id=str(getattr(source, "message_id", "") or ""),
            )
        return {
            "session_id": self._agent_health_session_id(session_key),
            "session_key": session_key,
            "platform": str(platform or ""),
            "jump_url": jump_url,
        }

    async def _check_output_silence(
        self,
        silence_timeout: Optional[float] = None,
        turn_deadline: Optional[float] = None,
    ) -> int:
        """Detect live turns with no confirmed user-visible content.

        This is deliberately separate from _check_session_stalls: pending
        inbound and AIAgent activity are not inputs.  Every candidate is
        re-read immediately before alert/interrupt to close the same stale
        snapshot race as the existing stall notifier.
        """
        from gateway.run import _STALL_NOTIFY_SEND_TIMEOUT_SECONDS
        from gateway.agent_health import (
            HealthEvent,
            should_emit_output_silence,
            should_enforce_turn_deadline,
        )

        threshold = (
            self._agent_health_silence_timeout_seconds()
            if silence_timeout is None
            else float(silence_timeout)
        )
        deadline = (
            self._agent_health_turn_deadline_seconds()
            if turn_deadline is None
            else float(turn_deadline)
        )
        if threshold <= 0 and deadline <= 0:
            return 0

        notified = getattr(self, "_output_silence_notified", None)
        if not isinstance(notified, dict):
            notified = {}
            self._output_silence_notified = notified
        enforced = getattr(self, "_turn_deadline_enforced", None)
        if not isinstance(enforced, dict):
            enforced = {}
            self._turn_deadline_enforced = enforced
        user_waiting = getattr(self, "_output_silence_user_waiting", None)
        if not isinstance(user_waiting, dict):
            user_waiting = {}
            self._output_silence_user_waiting = user_waiting
        starts = getattr(self, "_turn_started_at", None) or {}
        contents = getattr(self, "_last_content_sent_at", None) or {}
        emitted = 0

        for session_key, agent in list(
            (getattr(self, "_running_agents", None) or {}).items()
        ):
            if agent is None:
                continue
            state = self._peek_session_state(session_key)
            if state is None:
                continue
            generation = int(state.persistent.run_generation)
            latch_key = (session_key, generation)
            waiting_now = self._session_waiting_on_user(session_key)
            if waiting_now:
                user_waiting[latch_key] = True
                continue
            if user_waiting.get(latch_key):
                # User think-time is outside A.  Give the resumed agent a full
                # fresh window, and never alert on the same tick that observes
                # the wait ending.
                self._resume_output_silence_clock(session_key, generation)
                continue
            started_at = float(
                starts.get(session_key)
                or getattr(state.turn, "started_ts", 0.0)
                or 0.0
            )
            if started_at <= 0:
                continue
            last_output = float(contents.get(session_key, 0.0) or 0.0)
            silence = max(0.0, time.time() - max(started_at, last_output))

            deadline_due = should_enforce_turn_deadline(
                silence_seconds=silence,
                deadline=deadline,
                turn_live=True,
                already_enforced=bool(enforced.get(latch_key)),
                waiting_on_user=waiting_now,
            )
            warning_due = should_emit_output_silence(
                silence_seconds=silence,
                threshold=threshold,
                turn_live=True,
                already_notified=bool(notified.get(latch_key)),
                waiting_on_user=waiting_now,
            )
            if not deadline_due and not warning_due:
                continue

            # Re-read immediately before acting.  A streaming flush may have
            # landed while an earlier candidate in this pass was handled.
            if not self._is_session_run_current(session_key, generation):
                notified.pop(latch_key, None)
                enforced.pop(latch_key, None)
                continue
            fresh_state = self._peek_session_state(session_key)
            fresh_agent = fresh_state.turn.agent if fresh_state is not None else None
            if fresh_agent is None:
                continue
            fresh_waiting = self._session_waiting_on_user(session_key)
            if fresh_waiting:
                user_waiting[latch_key] = True
                continue
            if user_waiting.get(latch_key):
                self._resume_output_silence_clock(session_key, generation)
                continue
            fresh_started = float(
                (getattr(self, "_turn_started_at", None) or {}).get(session_key)
                or getattr(fresh_state.turn, "started_ts", 0.0)
                or 0.0
            )
            fresh_output = float(
                (getattr(self, "_last_content_sent_at", None) or {}).get(
                    session_key, 0.0
                )
                or 0.0
            )
            fresh_silence = max(
                0.0, time.time() - max(fresh_started, fresh_output)
            )
            source = self._get_cached_session_source(session_key)

            if should_enforce_turn_deadline(
                silence_seconds=fresh_silence,
                deadline=deadline,
                turn_live=True,
                already_enforced=bool(enforced.get(latch_key)),
                waiting_on_user=fresh_waiting,
            ):
                if source is None:
                    # Keep the latch clear and retry next tick; the exact reset
                    # funnel requires a routable SessionSource.
                    continue
                enforced[latch_key] = True
                notified[latch_key] = True
                try:
                    await self._interrupt_and_clear_session(
                        session_key,
                        source,
                        interrupt_reason="agent-health output deadline",
                        invalidation_reason="agent_health_turn_deadline",
                    )
                except Exception:
                    # Do not let monitoring failure wedge the watcher.  The
                    # next generation is already invalidated by the normal
                    # interrupt funnel when it progressed far enough.
                    pass

                source_notice_sent = False
                adapter = self._delivery_adapter_for(source)
                if adapter is not None:
                    minutes = max(1, int(deadline // 60))
                    user_text = (
                        f"⚠️ 이 요청은 {minutes}분 동안 채널로 전달된 응답이 "
                        "없어 자동 중단되었습니다. 다시 시도하거나 /reset으로 "
                        "새 세션을 시작해 주세요."
                    )
                    try:
                        metadata = dict(
                            self._thread_metadata_for_source(source) or {}
                        )
                        metadata["notify"] = True
                        result = await asyncio.wait_for(
                            adapter.send(
                                str(source.chat_id),
                                user_text,
                                metadata=metadata,
                            ),
                            timeout=_STALL_NOTIFY_SEND_TIMEOUT_SECONDS,
                        )
                        if getattr(result, "success", False):
                            self._last_content_sent_at[session_key] = time.time()
                            source_notice_sent = True
                    except Exception:
                        pass

                context = self._agent_health_event_context(session_key, source)
                self._emit_agent_health_event(
                    HealthEvent(
                        rule="A.turn_deadline",
                        category="A",
                        title="출력 무응답 하드 데드라인 강제 종료",
                        reason=(
                            f"턴이 {fresh_silence:.0f}초 동안 실제 채널 콘텐츠를 "
                            "전달하지 못했습니다."
                        ),
                        action=(
                            "동일 세션의 실행을 /reset 경로로 중단하고 원 스레드에 실패 사유를 게시했습니다."
                            if source_notice_sent
                            else "동일 세션의 실행을 /reset 경로로 중단했습니다. 원 스레드 실패 안내 전송은 실패하거나 시간 초과되었습니다."
                        ),
                        mention=True,
                        **context,
                    )
                )
                emitted += 1
                continue

            if not should_emit_output_silence(
                silence_seconds=fresh_silence,
                threshold=threshold,
                turn_live=True,
                already_notified=bool(notified.get(latch_key)),
                waiting_on_user=fresh_waiting,
            ):
                notified.pop(latch_key, None)
                continue
            context = self._agent_health_event_context(session_key, source)
            accepted = self._emit_agent_health_event(
                HealthEvent(
                    rule="A.output_silence",
                    category="A",
                    title="사용자 대기 중 채널 출력 무응답",
                    reason=(
                        f"실행 중인 턴이 {fresh_silence:.0f}초 동안 실제 채널 "
                        "콘텐츠를 한 건도 전달하지 않았습니다."
                    ),
                    action=(
                        (
                            f"{max(1, int(deadline // 60))}분 하드 데드라인까지 "
                            "출력이 없으면 턴을 자동 중단합니다."
                        )
                        if deadline > 0
                        else "하드 데드라인은 비활성화되어 있어 자동 중단하지 않습니다."
                    ),
                    mention=True,
                    **context,
                )
            )
            if accepted:
                notified[latch_key] = True
                emitted += 1

        return emitted
