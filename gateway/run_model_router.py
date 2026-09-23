"""Gateway model-route integration, ported onto the split runner mixin graph."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import threading
from collections import OrderedDict
from contextvars import copy_context
from typing import Any, Dict, Optional

from agent.async_utils import consume_detached_task_result
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource

logger = logging.getLogger("gateway.run")
_MODEL_ROUTER_SHADOW_MAX_INFLIGHT = 4
_MODEL_ROUTER_INIT_LOCK = threading.Lock()


def _load_gateway_config():
    from gateway.run import _load_gateway_config as loader
    return loader()


def _model_router_mode(config: Optional[dict] = None) -> str:
    """Return effective router mode without importing the router module.

    ``HERMES_MODEL_ROUTER_MODE`` is an emergency bridge and wins when set.
    Unknown non-empty overrides fail safe to ``off``.
    """
    override = os.environ.get("HERMES_MODEL_ROUTER_MODE", "").strip().lower()
    if override:
        return override if override in {"shadow", "enforce"} else "off"
    cfg = config if isinstance(config, dict) else _load_gateway_config()
    section = cfg.get("model_routes") if isinstance(cfg, dict) else None
    router = section.get("router") if isinstance(section, dict) else None
    raw_mode = router.get("mode") if isinstance(router, dict) else None
    mode = str(raw_mode or "").strip().lower() if isinstance(raw_mode, str) else "off"
    return mode if mode in {"shadow", "enforce"} else "off"


def _mood_injection_enabled(config: Optional[dict]) -> bool:
    """Fast raw-config gate matching ``MoodsConfig.enabled`` parse semantics."""
    section = config.get("model_routes") if isinstance(config, dict) else None
    moods = section.get("moods") if isinstance(section, dict) else None
    return isinstance(moods, dict) and moods.get("enabled") is True


def _mood_prompt_suffix(record: dict, moods: Any) -> str:
    """Return this decision's call-time mood suffix and update its audit field."""
    if not bool(getattr(moods, "enabled", False)):
        record["mood_applied"] = "shadow"
        return ""

    record["mood_applied"] = "none"
    mood = record.get("mood")
    from gateway.model_router import MOOD_VALUES

    if not isinstance(mood, str) or mood not in MOOD_VALUES:
        return ""

    confidence = record.get("mood_confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or float(confidence) < float(getattr(moods, "confidence_threshold", 0.7))
    ):
        return ""

    from gateway.mood_loader import load_mood_file

    content = load_mood_file(moods, mood)
    if content is None:
        return ""

    record["mood_applied"] = "injection"
    return f"\n\n# Current Mood: {mood}\n{content}"



class GatewayModelRouterMixin:
    def _ensure_model_router_runtime_state(self):
        """Return lazily initialized guards for bare-test and live runners."""
        if all(
            getattr(self, name, None) is not None
            for name in (
                "_model_router_state_lock",
                "_model_router_state",
                "_model_router_shadow_slots",
                "_model_router_shadow_inflight",
            )
        ):
            return (
                self._model_router_state_lock,
                self._model_router_state,
                self._model_router_shadow_slots,
                self._model_router_shadow_inflight,
            )

        with _MODEL_ROUTER_INIT_LOCK:
            if getattr(self, "_model_router_state_lock", None) is None:
                self._model_router_state_lock = threading.Lock()
            if getattr(self, "_model_router_state", None) is None:
                self._model_router_state = {}
            if getattr(self, "_model_router_shadow_slots", None) is None:
                self._model_router_shadow_slots = threading.BoundedSemaphore(
                    _MODEL_ROUTER_SHADOW_MAX_INFLIGHT
                )
            if getattr(self, "_model_router_shadow_inflight", None) is None:
                self._model_router_shadow_inflight = set()
        return (
            self._model_router_state_lock,
            self._model_router_state,
            self._model_router_shadow_slots,
            self._model_router_shadow_inflight,
        )

    @staticmethod
    def _model_route_catalog_fingerprint(cfg: dict) -> str:
        """Hash only config roots that affect route parsing and validation."""
        relevant = {
            "model_routes": cfg.get("model_routes"),
            "providers": cfg.get("providers"),
            "custom_providers": cfg.get("custom_providers"),
            "router_mode_env": os.environ.get(
                "HERMES_MODEL_ROUTER_MODE", ""
            ).strip().lower(),
        }
        try:
            serialized = json.dumps(
                relevant,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except (TypeError, ValueError):
            # Malformed hand-written YAML can contain mixed-type mapping keys,
            # which JSON cannot sort. Validation still needs to see that input.
            serialized = repr(relevant)
        payload = serialized.encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _model_route_catalog(self, cfg: dict):
        """Return the parsed route catalog until relevant config changes."""
        from hermes_cli.model_routes import load_routes

        fingerprint = self._model_route_catalog_fingerprint(cfg)
        lock = getattr(self, "_model_route_catalog_cache_lock", None)
        cache = getattr(self, "_model_route_catalog_cache", None)
        if lock is None or cache is None:
            with _MODEL_ROUTER_INIT_LOCK:
                if getattr(self, "_model_route_catalog_cache_lock", None) is None:
                    self._model_route_catalog_cache_lock = threading.Lock()
                if getattr(self, "_model_route_catalog_cache", None) is None:
                    self._model_route_catalog_cache = OrderedDict()
            lock = self._model_route_catalog_cache_lock
            cache = self._model_route_catalog_cache

        with lock:
            catalog = cache.get(fingerprint)
            if catalog is not None:
                cache.move_to_end(fingerprint)
                return catalog
            catalog = load_routes(cfg)
            cache[fingerprint] = catalog
            cache.move_to_end(fingerprint)
            while len(cache) > 16:
                cache.popitem(last=False)
            return catalog

    def _begin_model_router_state(self, session_key: str, *, invalidate: bool):
        """Create an isolated state transaction for one routing decision."""
        lock, shared, _slots, _inflight = self._ensure_model_router_runtime_state()
        key = str(session_key or "unknown")
        with lock:
            current = shared.get(key)
            if not isinstance(current, dict):
                current = {"normal_streak": 0}
                shared[key] = current
            elif invalidate:
                # Enforce supersedes any older detached shadow transaction.
                current = dict(current)
                shared[key] = current
            local = {key: dict(current)}
        return key, shared, current, local

    def _commit_model_router_state(
        self,
        key: str,
        shared: dict,
        identity: dict,
        local: dict,
    ) -> bool:
        """Commit only if no boundary or newer decision replaced this entry."""
        lock, current_shared, _slots, _inflight = (
            self._ensure_model_router_runtime_state()
        )
        with lock:
            if current_shared is not shared or shared.get(key) is not identity:
                return False
            entry = local.get(key)
            if not isinstance(entry, dict):
                return False
            shared[key] = entry
            return True

    def _schedule_model_router_shadow(
        self,
        *,
        event: MessageEvent,
        session_key: str,
        runtime: dict,
        user_config: Optional[dict] = None,
    ) -> bool:
        """Start bounded fire-and-forget shadow evaluation.

        Only one task per session and at most four tasks per gateway may be in
        flight. Saturation drops shadow observations instead of queueing work
        behind the user-facing turn.
        """
        cfg = user_config if isinstance(user_config, dict) else {}
        if _model_router_mode(cfg) != "shadow":
            return False
        # Enabled mood injection needs this turn's decision before its API
        # call. The pre-dispatch stage owns that synchronous path; leaving the
        # detached observer active would duplicate classification and could
        # stage a suffix too late for the turn it describes.
        if _mood_injection_enabled(cfg):
            return False

        lock, shared, slots, inflight = self._ensure_model_router_runtime_state()
        if not slots.acquire(blocking=False):
            logger.debug("model router: shadow capacity full; observation skipped")
            return False

        key = str(session_key or "unknown")
        with lock:
            if key in inflight:
                slots.release()
                logger.debug(
                    "model router: shadow observation already running for %s",
                    key,
                )
                return False
            inflight.add(key)
            identity = shared.get(key)
            if not isinstance(identity, dict):
                identity = {"normal_streak": 0}
                shared[key] = identity
            local_state = {key: dict(identity)}

        def evaluate() -> None:
            try:
                decision = self._evaluate_model_router_shadow(
                    event=event,
                    session_key=session_key,
                    runtime=runtime,
                    user_config=cfg,
                    state_override=local_state,
                )
                if decision is not None and _model_router_mode(cfg) == "shadow":
                    self._commit_model_router_state(
                        key, shared, identity, local_state
                    )
            except Exception as exc:
                logger.warning(
                    "model router shadow evaluation failed open for session=%s (%s)",
                    session_key or "?",
                    type(exc).__name__,
                )
            finally:
                with lock:
                    inflight.discard(key)
                slots.release()

        try:
            worker_context = copy_context()
            threading.Thread(
                target=worker_context.run,
                args=(evaluate,),
                daemon=True,
                name=f"model-router-shadow-{key[:24]}",
            ).start()
        except Exception as exc:
            with lock:
                inflight.discard(key)
            slots.release()
            logger.warning(
                "model router shadow worker failed to start (%s)",
                type(exc).__name__,
            )
            return False
        return True

    def _model_router_runtime_snapshot(
        self,
        source: SessionSource,
        session_key: str,
        *,
        user_config: Optional[dict] = None,
    ) -> dict:
        """Return the current non-secret runtime axes for route evaluation."""
        try:
            model, runtime_kwargs = self._resolve_session_agent_runtime(
                source=source,
                session_key=session_key,
                user_config=user_config,
            )
        except Exception:
            logger.debug("model router: runtime snapshot failed", exc_info=True)
            return {}
        snapshot: dict = {}
        if model:
            snapshot["model"] = model
        for name in ("provider", "base_url"):
            value = (runtime_kwargs or {}).get(name)
            if value:
                snapshot[name] = value
        try:
            reasoning = self._resolve_session_reasoning_config(
                source=source,
                session_key=session_key,
                model=str(model or ""),
            )
        except Exception:
            logger.debug("model router: reasoning snapshot failed", exc_info=True)
            reasoning = None
        if isinstance(reasoning, dict):
            if reasoning.get("selection") == "pinned":
                snapshot["reasoning_selection"] = "pinned"
            if reasoning.get("enabled") is False:
                snapshot["reasoning_effort"] = "none"
            elif reasoning.get("effort"):
                snapshot["reasoning_effort"] = str(reasoning["effort"])
        return snapshot

    def _set_active_model_route(self, session_key: str, route_name: str) -> None:
        """Persist applied route intent for agent rebuilds in this conversation."""
        if not session_key:
            return
        self._session_state(session_key).conversation.active_route_name = str(
            route_name or ""
        ).strip()

    def _bind_active_model_route(self, agent: Any, session_key: str) -> None:
        """Copy conversation route intent onto a newly built or cached agent."""
        state = self._peek_session_state(session_key) if session_key else None
        route_name = state.conversation.active_route_name if state else ""
        agent._active_route_name = str(route_name or "").strip()

    @staticmethod
    def _selected_model_route(decision: Any, router: Any) -> str:
        """Recover the selected route for no-op decisions without model scans."""
        directive = getattr(decision, "directive", None)
        if isinstance(directive, dict):
            return str(directive.get("route") or "").strip()
        if getattr(decision, "rule", None):
            return str(getattr(decision, "label", "") or "").strip()
        outcome = str(getattr(decision, "outcome", "") or "")
        label = str(getattr(decision, "label", "") or "")
        record = getattr(decision, "record", None) or {}
        if record.get("refusal_applied"):
            refusal = getattr(router, "refusal", None)
            if label in {"SYSTEM_DEV", "FRONTEND_DEV"}:
                return str(getattr(refusal, "dev_route", "") or "").strip()
            if label == "DOCUMENT_WORK":
                return str(
                    getattr(refusal, "document_route", "")
                    or getattr(refusal, "chat_route", "")
                    or ""
                ).strip()
            return str(getattr(refusal, "chat_route", "") or "").strip()
        if label == "NORMAL" and (
            outcome in {"noop_already_chat", "repromote_held"}
            or outcome.startswith("noop_satisfied_repromote_")
        ):
            return str(getattr(router, "chat_route", "") or "").strip()
        label_routes = dict(getattr(router, "label_routes", None) or {})
        return str(label_routes.get(label) or "").strip()

    async def _classify_model_router_with_budget(
        self,
        *,
        model_router: Any,
        event: MessageEvent,
        session_key: str,
        runtime: dict,
        cfg: dict,
        catalog: Any,
        mode: str,
        state: dict,
    ):
        """Classify within the enforce deadline, then apply state once.

        The overdue worker performs classification only. Its eventual result is
        discarded, so it cannot mutate hysteresis or apply a late directive.
        """
        context = await asyncio.to_thread(
            model_router.build_context,
            event=event,
            session_store=self.session_store,
            runtime=runtime,
            recent_turn_limit=int(
                getattr(catalog.router, "recent_turns", 5) or 5
            ),
            loaded_skills=[],
            session_key_override=session_key,
        )
        provider, model, transport_timeout, budget = (
            model_router._classifier_request_settings(catalog.router)
        )
        classify_task = asyncio.create_task(
            asyncio.to_thread(
                model_router.classify_dev_detailed,
                context,
                provider=provider,
                model=model,
                timeout=min(transport_timeout, budget),
            )
        )
        try:
            done, _pending = await asyncio.wait(
                {classify_task}, timeout=budget
            )
        except asyncio.CancelledError:
            classify_task.cancel()
            classify_task.add_done_callback(consume_detached_task_result)
            raise

        if classify_task in done:
            detail = await classify_task
        else:
            classify_task.cancel()
            classify_task.add_done_callback(consume_detached_task_result)
            detail = model_router._fallback_classification(
                context, "classifier_timeout"
            )
            logger.warning(
                "model router: classifier exceeded %.3fs; using fallback",
                budget,
            )

        return await asyncio.to_thread(
            model_router.classifier_decision_from_detail,
            context=context,
            detail=detail,
            session_store=self.session_store,
            runtime=runtime,
            cfg=cfg,
            catalog=catalog,
            router=catalog.router,
            mode=mode,
            state=state,
            provider=provider,
            model=model,
        )

    async def _model_router_stage(
        self,
        event: MessageEvent,
        source: SessionSource,
        session_key: str,
        *,
        mode: str,
        user_config: Optional[dict] = None,
    ):
        """Evaluate routing and apply directives only when mode is enforce.

        Shadow is scheduled fire-and-forget by the off-loop turn builder. This
        pre-dispatch rail exists for live enforce decisions so a successful
        route selection can rebuild the agent before the current turn starts
        and carry the exact route intent with it.
        """
        from gateway import model_router

        mode = str(mode or "").strip().lower()
        if mode not in {"shadow", "enforce"}:
            return None
        raw_text = str(getattr(event, "text", None) or "")
        text = raw_text.strip()
        if not text:
            return None

        cfg = user_config if isinstance(user_config, dict) else _load_gateway_config()
        catalog = self._model_route_catalog(cfg)
        if str(getattr(catalog.router, "mode", "") or "").lower() != mode:
            return None
        matched = model_router.match_static_rule(
            list(catalog.static_rules or []),
            text=raw_text,
            source_context=model_router._source_dict(event),
        )
        if matched is None and text.startswith("/"):
            return None

        state_key, shared_state, state_identity, local_state = (
            self._begin_model_router_state(
                session_key,
                invalidate=mode == "enforce",
            )
        )

        runtime = self._model_router_runtime_snapshot(
            source,
            session_key,
            user_config=cfg,
        )
        if matched is not None:
            rule, rule_name = matched
            decision = await asyncio.to_thread(
                model_router.static_rule_decision,
                rule=rule,
                rule_name=rule_name,
                text=raw_text,
                session_key=session_key,
                runtime=runtime,
                cfg=cfg,
                catalog=catalog,
                router=catalog.router,
                mode=mode,
                state=local_state,
            )
        else:
            decision = await self._classify_model_router_with_budget(
                model_router=model_router,
                event=event,
                session_key=session_key,
                runtime=runtime,
                cfg=cfg,
                catalog=catalog,
                mode=mode,
                state=local_state,
            )

        state_committed = self._commit_model_router_state(
            state_key,
            shared_state,
            state_identity,
            local_state,
        )
        self._set_pending_mood_prompt(session_key, "")
        if state_committed:
            mood_suffix = _mood_prompt_suffix(decision.record, catalog.moods)
            self._set_pending_mood_prompt(session_key, mood_suffix)
        else:
            decision.record["mood_applied"] = (
                "none" if bool(getattr(catalog.moods, "enabled", False)) else "shadow"
            )
        directive = decision.directive
        if mode == "enforce":
            decision.record["applied"] = False
        if (
            mode == "enforce"
            and state_committed
            and directive
            and decision.outcome in {
                "switch",
                "downgrade_to_chat",
                "repromote_to_primary",
                "refusal_switch",
            }
        ):
            reasoning_effort = str(directive.get("reasoning_effort") or "")
            try:
                applied, reasoning_applied = await self._apply_model_router_directive(
                    session_key,
                    directive,
                    cfg,
                    source=source,
                )
            except Exception:
                logger.warning(
                    "model router: applying directive %s failed",
                    directive,
                    exc_info=True,
                )
                applied, reasoning_applied = False, False
            decision.record["applied"] = bool(applied)
            if reasoning_effort:
                decision.record["reasoning_applied"] = bool(reasoning_applied)
            if applied:
                logger.info(
                    "model router: %s session=%s -> route=%s model=%s (%s)",
                    decision.outcome,
                    session_key,
                    directive.get("route"),
                    directive.get("model"),
                    decision.label,
                )
                if (
                    decision.outcome == "refusal_switch"
                    and not decision.record.get("forced_refusal_route")
                    and bool(
                        getattr(
                            getattr(catalog.router, "refusal", None),
                            "notify",
                            True,
                        )
                    )
                ):
                    try:
                        masked = int(decision.record.get("masked") or 0)
                        notice = (
                            f"⚠️ 거절 감지 → {directive.get('route')}"
                            f"({directive.get('model')}) 라우팅 (masked={masked})"
                        )
                        await self._deliver_platform_notice(source, notice)
                    except Exception:
                        logger.debug(
                            "model router: refusal-risk notice send failed",
                            exc_info=True,
                        )
        elif mode == "enforce" and state_committed and (
            decision.outcome in {
                "noop_satisfied",
                "noop_already_chat",
                "repromote_held",
            }
            or decision.outcome.startswith("noop_satisfied_repromote_")
        ):
            # A no-op still selected a concrete route; retain that explicit
            # intent rather than reconstructing it later from model membership.
            selected_route = self._selected_model_route(decision, catalog.router)
            if selected_route:
                self._set_active_model_route(session_key, selected_route)

        await asyncio.to_thread(
            model_router.log_decision,
            decision.record,
            decision_log=catalog.router.decision_log,
        )
        return decision

    async def _handle_gateway_hard_refusal(
        self,
        session_key: str,
        session_id: str,
        source: SessionSource,
        agent_result: dict,
    ) -> int:
        """Mask this hard-refused turn and stage one permissive next-turn hop."""
        if not str(agent_result.get("error") or "").startswith(
            "content_policy_blocked"
        ):
            return 0

        from hermes_cli.model_routes import load_routes as _load_model_routes

        try:
            cfg = _load_gateway_config()
            catalog = _load_model_routes(cfg)
            refusal = catalog.router.refusal
            if not bool(getattr(refusal, "enabled", False)):
                return 0
        except Exception:
            logger.warning(
                "model router: hard-refusal config load failed session=%s",
                session_key,
                exc_info=True,
            )
            return 0

        return await self._stage_gateway_refusal_recovery(
            session_key,
            session_id,
            source,
            cfg=cfg,
            catalog=catalog,
            refusal=refusal,
            kind="hard",
        )

    def _reset_gateway_refusal_recovery(self, session_key: str) -> None:
        """Clear the consecutive-refusal guard after one clean agent turn."""
        state = getattr(self, "_model_router_state", None)
        if state is None:
            state = {}
            self._model_router_state = state
        entry = state.setdefault(session_key or "unknown", {"normal_streak": 0})
        entry["refusal_recovery_count"] = 0
        entry["refusal_recovery_exhausted"] = False

    async def _handle_gateway_soft_refusal(
        self,
        session_key: str,
        session_id: str,
        source: SessionSource,
        agent_result: dict,
        event_text: str,
    ) -> int:
        """Probe a completed assistant response and stage permissive recovery."""
        error = str(agent_result.get("error") or "")
        if error.startswith("content_policy_blocked"):
            # The hard-refusal path owns this turn, including the guard count.
            return 0
        if (
            agent_result.get("interrupted")
            or agent_result.get("failed")
            or agent_result.get("partial")
            or error
        ):
            return 0

        response_text = str(agent_result.get("final_response") or "").strip()
        if not response_text:
            return 0

        from gateway import model_router as _model_router
        from hermes_cli.model_routes import load_routes as _load_model_routes

        try:
            cfg = _load_gateway_config()
            catalog = _load_model_routes(cfg)
            refusal = catalog.router.refusal
        except Exception:
            logger.warning(
                "model router: soft-refusal config load failed session=%s",
                session_key,
                exc_info=True,
            )
            return 0

        if not (
            bool(getattr(refusal, "enabled", False))
            and bool(getattr(refusal, "mask_on_refusal", True))
            and bool(getattr(refusal, "soft_detect", True))
        ):
            self._reset_gateway_refusal_recovery(session_key)
            return 0
        if len(response_text) < 40:
            self._reset_gateway_refusal_recovery(session_key)
            return 0

        detail = await asyncio.to_thread(
            _model_router.classify_prior_refusal,
            response_text,
            str(event_text or ""),
            cfg,
            catalog,
        )
        try:
            confidence = float(detail.get("prior_refusal_confidence"))
        except (TypeError, ValueError):
            confidence = None
        threshold = float(getattr(refusal, "min_confidence", 0.85))
        if (
            detail.get("prior_refusal") is not True
            or confidence is None
            or confidence < threshold
        ):
            self._reset_gateway_refusal_recovery(session_key)
            return 0

        return await self._stage_gateway_refusal_recovery(
            session_key,
            session_id,
            source,
            cfg=cfg,
            catalog=catalog,
            refusal=refusal,
            kind="soft",
        )

    async def _stage_gateway_refusal_recovery(
        self,
        session_key: str,
        session_id: str,
        source: SessionSource,
        *,
        cfg: dict,
        catalog: object,
        refusal: object,
        kind: str,
    ) -> int:
        """Mask one refused turn and stage a guarded permissive-route hop."""
        from gateway import model_router as _model_router

        state = getattr(self, "_model_router_state", None)
        if state is None:
            state = {}
            self._model_router_state = state
        entry = state.setdefault(session_key or "unknown", {"normal_streak": 0})
        max_hops = max(1, int(getattr(refusal, "max_recovery_hops", 2) or 2))
        recovery_count = int(entry.get("refusal_recovery_count") or 0)
        if recovery_count >= max_hops:
            if not bool(entry.get("refusal_recovery_exhausted")):
                entry["refusal_recovery_exhausted"] = True
                try:
                    await self._deliver_platform_notice(
                        source,
                        "⚠️ 거절이 반복 — 라우팅으로 해결되는 케이스가 아님. 자동 전환을 멈춤",
                    )
                except Exception:
                    logger.debug(
                        "model router: refusal exhaustion notice send failed",
                        exc_info=True,
                    )
            return 0

        entry["refusal_recovery_count"] = recovery_count + 1
        entry["refusal_recovery_exhausted"] = False
        masked = 0
        if bool(getattr(refusal, "mask_on_refusal", True)):
            try:
                db = getattr(self.session_store, "_db", None)
                rows = (
                    await asyncio.to_thread(db.get_messages, session_id)
                    if db is not None
                    else []
                )
                latest_user_id = next(
                    (
                        int(message["id"])
                        for message in reversed(rows)
                        if message.get("role") == "user"
                    ),
                    None,
                )
                assistant_ids = [
                    int(message["id"])
                    for message in rows
                    if latest_user_id is not None
                    and int(message.get("id") or 0) > latest_user_id
                    and message.get("role") == "assistant"
                    and str(message.get("content") or "").strip()
                ]
                masked = await self.async_session_store.deactivate_messages(
                    session_id, assistant_ids,
                )
            except Exception:
                logger.warning(
                    "model router: %s-refusal masking failed session=%s",
                    kind, session_key,
                    exc_info=True,
                )

        entry["force_refusal_route"] = True
        entry["force_refusal_reason"] = "prior_turn_refused"

        if bool(getattr(refusal, "notify", True)):
            try:
                label = str(entry.get("last_label") or "NORMAL")
                route_name = _model_router.refusal_route_for_label(refusal, label)
                directive = await asyncio.to_thread(
                    _model_router._resolve_route_directive,
                    route_name,
                    cfg,
                    catalog,
                )
                route = str(
                    (directive or {}).get("route") or route_name or "PERMISSIVE"
                )
                model = str((directive or {}).get("model") or "staged")
                detected = "거절 감지" if kind == "hard" else "응답 거절 감지"
                await self._deliver_platform_notice(
                    source,
                    f"⚠️ {detected} → 메시지 가림({masked}) · 다음 턴 {route}({model})",
                )
            except Exception:
                logger.debug(
                    "model router: %s-refusal notice send failed", kind, exc_info=True,
                )
        return masked

    async def _apply_model_router_directive(
        self,
        session_key: str,
        directive: dict,
        cfg: dict,
        *,
        source: Optional[SessionSource] = None,
    ) -> tuple[bool, bool]:
        """Apply a resolved route before dispatch and retain its exact intent."""
        reasoning = self._resolve_session_reasoning_config(session_key=session_key)
        if isinstance(reasoning, dict) and reasoning.get("selection") == "pinned":
            return False, False
        from hermes_cli.model_switch import switch_model

        model_cfg = cfg.get("model", {}) if isinstance(cfg, dict) else {}
        if not isinstance(model_cfg, dict):
            model_cfg = {}
        current_model = model_cfg.get("default", "")
        current_provider = model_cfg.get("provider", "openrouter")
        current_base_url = model_cfg.get("base_url", "")
        current_api_key = ""
        user_providers = cfg.get("providers") if isinstance(cfg, dict) else None
        try:
            from hermes_cli.config import get_compatible_custom_providers

            custom_providers = get_compatible_custom_providers(cfg)
        except Exception:
            custom_providers = (
                cfg.get("custom_providers") if isinstance(cfg, dict) else None
            )
        state = self._peek_session_state(session_key)
        override = state.conversation.model_override if state else None
        if override:
            current_model = override.get("model", current_model)
            current_provider = override.get("provider", current_provider)
            current_base_url = override.get("base_url", current_base_url)
            current_api_key = override.get("api_key", current_api_key)

        result = await asyncio.to_thread(
            switch_model,
            raw_input=str(directive.get("model") or ""),
            current_provider=current_provider,
            current_model=current_model,
            current_base_url=current_base_url,
            current_api_key=current_api_key,
            is_global=False,
            explicit_provider=str(directive.get("provider") or "") or None,
            user_providers=user_providers,
            custom_providers=custom_providers,
        )
        if not result.success:
            logger.warning(
                "model router: switch to %s@%s failed: %s",
                directive.get("model"),
                directive.get("provider"),
                result.error_message,
            )
            return False, False

        session_db = getattr(self, "_session_db", None)
        async_store = getattr(self, "async_session_store", None)
        if session_db is not None and source is not None and async_store is not None:
            try:
                session_entry = await async_store.get_or_create_session(source)
                await session_db.update_session_model(
                    session_entry.session_id,
                    result.new_model,
                )
            except Exception:
                logger.debug(
                    "model router: persist model switch to session DB failed",
                    exc_info=True,
                )

        route_name = str(directive.get("route") or "").strip()
        route_reason = str(directive.get("reason") or "").strip()
        if not hasattr(self, "_pending_model_notes"):
            self._pending_model_notes = {}
        self._pending_model_notes[session_key] = (
            f"[Note: model was just switched from {current_model} to "
            f"{result.new_model} by the model router (route '{route_name}'"
            + (
                f": {route_reason}"
                if route_reason and route_reason != route_name
                else ""
            )
            + "). Adjust your self-identification accordingly.]"
        )

        self._session_state(session_key).conversation.model_override = {
            "model": result.new_model,
            "provider": result.target_provider,
            "api_key": result.api_key,
            "base_url": result.base_url,
            "api_mode": result.api_mode,
        }
        # Record route intent only after the runtime switch commits.  This is
        # the source of truth consumed by route-aware outage fallback; never
        # infer it later from ambiguous accepted-model membership.
        self._set_active_model_route(session_key, route_name)
        if async_store is not None:
            try:
                await async_store.set_model_override(
                    session_key,
                    self._session_state(session_key).conversation.model_override,
                )
            except Exception:
                logger.debug(
                    "model router: persist session override failed",
                    exc_info=True,
                )

        reasoning_applied = False
        effort = str(directive.get("reasoning_effort") or "")
        if effort:
            from hermes_constants import parse_reasoning_effort

            parsed = parse_reasoning_effort(effort)
            if parsed is not None:
                self._set_session_reasoning_override(session_key, parsed)
                reasoning_applied = True

        self._evict_cached_agent(session_key)
        return True, reasoning_applied

    def _evaluate_model_router_shadow(
        self,
        *,
        event: MessageEvent,
        session_key: str,
        runtime: dict,
        user_config: Optional[dict] = None,
        state_override: Optional[dict] = None,
    ):
        """Evaluate and audit M3 routing without mutating live routing state.

        A bounded daemon worker calls this after ``TurnRunner.run_sync`` has
        resolved the canonical runtime. It intentionally recognizes only
        ``mode: shadow``; enforce decisions use the pre-dispatch
        ``_model_router_stage`` so an applied switch is visible before agent
        construction.
        Route health resolution is read-only: shadow evaluation may append its
        decision log, but cannot run a recovery probe or rewrite shared health.
        """
        cfg = user_config if isinstance(user_config, dict) else {}
        mode = _model_router_mode(cfg)
        if mode != "shadow":
            return None

        from gateway import model_router

        catalog = self._model_route_catalog(cfg)
        if catalog.router.mode != "shadow":
            return None
        if isinstance(state_override, dict):
            state = state_override
        else:
            _lock, state, _slots, _inflight = (
                self._ensure_model_router_runtime_state()
            )
        decision = model_router.evaluate_event(
            event=event,
            session_store=self.session_store,
            runtime=runtime,
            cfg=cfg,
            catalog=catalog,
            router=catalog.router,
            mode="shadow",
            state=state,
            session_key_override=session_key,
        )
        if decision is not None:
            model_router.log_decision(
                decision.record,
                decision_log=catalog.router.decision_log,
            )
        return decision
    def _set_pending_mood_prompt(self, session_key: str, prompt: str) -> None:
        """Stage one call-time mood suffix, or clear a stale staged suffix."""
        if not session_key:
            return
        prompts = getattr(self, "_pending_mood_prompts", None)
        if not isinstance(prompts, dict):
            prompts = {}
            self._pending_mood_prompts = prompts
        if prompt:
            prompts[session_key] = prompt
        else:
            prompts.pop(session_key, None)

    def _consume_pending_mood_prompt(self, session_key: str) -> str:
        """Consume the current turn's mood suffix exactly once."""
        prompts = getattr(self, "_pending_mood_prompts", None)
        if not session_key or not isinstance(prompts, dict):
            return ""
        prompt = prompts.pop(session_key, "")
        return prompt if isinstance(prompt, str) else ""
