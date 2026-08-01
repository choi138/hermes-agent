"""Process-wide single-flight coordinator for automatic background reviews.

Automatic memory/skill review used to create one daemon thread per trigger.
During delegated review-heavy work those threads multiplied and competed with
the next user turn.  This coordinator provides one queue and one worker for the
process, exact-snapshot deduplication, pending-request coalescing, and an idle
grace period before auxiliary model work begins.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional

from agent.background_review_policy import is_primary_foreground_agent

logger = logging.getLogger(__name__)

_MEMORY_FLAG = 1
_SKILLS_FLAG = 2


def _review_flags(review_memory: bool, review_skills: bool) -> int:
    return (_MEMORY_FLAG if review_memory else 0) | (
        _SKILLS_FLAG if review_skills else 0
    )


def review_snapshot_fingerprint(messages_snapshot: List[Dict[str, Any]]) -> str:
    """Return a content hash without retaining or logging transcript text."""

    try:
        payload = json.dumps(
            messages_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8", errors="replace")
    except Exception:
        payload = repr(messages_snapshot).encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


@dataclass
class _ReviewRequest:
    key: str
    flags: int
    target_factory: Callable[[bool, bool], Callable[[], Any]]
    defer_for_idle: bool


class BackgroundReviewCoordinator:
    """Serialize and deduplicate background-review work within one process."""

    def __init__(
        self,
        *,
        idle_grace_seconds: float = 2.0,
        dedupe_ttl_seconds: float = 3600.0,
        queue_limit: int = 64,
    ) -> None:
        self._condition = threading.Condition()
        self._pending: "OrderedDict[str, _ReviewRequest]" = OrderedDict()
        self._completed: Dict[str, tuple[float, int]] = {}
        self._active_key: Optional[str] = None
        self._active_flags = 0
        self._worker_alive = False
        self._foreground_turns = 0
        self._last_foreground_finish = 0.0
        self._idle_grace_seconds = max(float(idle_grace_seconds), 0.0)
        self._dedupe_ttl_seconds = max(float(dedupe_ttl_seconds), 0.0)
        self._queue_limit = max(int(queue_limit), 1)

    def configure(
        self,
        *,
        idle_grace_seconds: float,
        dedupe_ttl_seconds: float,
        queue_limit: int,
    ) -> None:
        """Apply current config without replacing an active coordinator."""

        with self._condition:
            self._idle_grace_seconds = max(float(idle_grace_seconds), 0.0)
            self._dedupe_ttl_seconds = max(float(dedupe_ttl_seconds), 0.0)
            self._queue_limit = max(int(queue_limit), 1)
            self._condition.notify_all()

    def foreground_started(self) -> None:
        with self._condition:
            self._foreground_turns += 1
            self._condition.notify_all()

    def foreground_finished(self) -> None:
        with self._condition:
            self._foreground_turns = max(self._foreground_turns - 1, 0)
            self._last_foreground_finish = time.monotonic()
            self._condition.notify_all()

    def _expire_completed_locked(self, now: float) -> None:
        if self._dedupe_ttl_seconds <= 0:
            self._completed.clear()
            return
        cutoff = now - self._dedupe_ttl_seconds
        expired = [key for key, (finished, _flags) in self._completed.items() if finished < cutoff]
        for key in expired:
            self._completed.pop(key, None)

    def submit(
        self,
        *,
        owner_token: str,
        messages_snapshot: List[Dict[str, Any]],
        review_memory: bool,
        review_skills: bool,
        target_factory: Callable[[bool, bool], Callable[[], Any]],
    ) -> str:
        """Queue review work and return its disposition.

        Return values are ``queued``, ``coalesced``, ``deduplicated``, or
        ``queue_full``.  A duplicate is considered accepted because equivalent
        work is already running, queued, or recently completed.
        """

        requested = _review_flags(review_memory, review_skills)
        if requested == 0:
            return "deduplicated"

        fingerprint = review_snapshot_fingerprint(messages_snapshot)
        key = f"{owner_token}:{fingerprint}"
        thread_to_start = None

        with self._condition:
            now = time.monotonic()
            self._expire_completed_locked(now)

            completed_flags = self._completed.get(key, (0.0, 0))[1]
            missing = requested & ~completed_flags
            if missing == 0:
                return "deduplicated"

            if self._active_key == key:
                missing &= ~self._active_flags
                if missing == 0:
                    return "deduplicated"

            pending = self._pending.get(key)
            if pending is not None:
                before = pending.flags
                pending.flags |= missing
                pending.target_factory = target_factory
                pending.defer_for_idle = pending.defer_for_idle or self._foreground_turns > 0
                return "coalesced" if pending.flags != before else "deduplicated"

            if len(self._pending) >= self._queue_limit:
                logger.warning(
                    "Background review queue is full (%d); deferring review to a future turn",
                    self._queue_limit,
                )
                return "queue_full"

            self._pending[key] = _ReviewRequest(
                key=key,
                flags=missing,
                target_factory=target_factory,
                defer_for_idle=self._foreground_turns > 0,
            )
            if not self._worker_alive:
                self._worker_alive = True
                if self._foreground_turns > 0:
                    # A real Thread.start() is asynchronous, but several unit
                    # tests intentionally replace Thread with an inline fake.
                    # A zero-delay Timer preserves asynchronous semantics here
                    # and, in production, lets the foreground call unwind
                    # before the worker begins its idle wait.
                    thread_to_start = threading.Timer(0, self._worker)
                    thread_to_start.daemon = True
                    thread_to_start.name = "bg-review-coordinator"
                else:
                    thread_to_start = threading.Thread(
                        target=self._worker,
                        daemon=True,
                        name="bg-review-coordinator",
                    )

        if thread_to_start is not None:
            try:
                thread_to_start.start()
            except Exception:
                with self._condition:
                    self._worker_alive = False
                    self._condition.notify_all()
                raise
        return "queued"

    def _worker(self) -> None:
        while True:
            with self._condition:
                if not self._pending:
                    self._worker_alive = False
                    self._condition.notify_all()
                    return

                request = next(iter(self._pending.values()))
                while self._foreground_turns > 0:
                    self._condition.wait()

                if request.defer_for_idle and self._idle_grace_seconds > 0:
                    remaining = (
                        self._last_foreground_finish
                        + self._idle_grace_seconds
                        - time.monotonic()
                    )
                    if remaining > 0:
                        self._condition.wait(timeout=remaining)
                        continue

                self._pending.pop(request.key, None)
                self._active_key = request.key
                self._active_flags = request.flags

            succeeded = False
            try:
                target = request.target_factory(
                    bool(request.flags & _MEMORY_FLAG),
                    bool(request.flags & _SKILLS_FLAG),
                )
                outcome = target()
                succeeded = outcome is not False
            except Exception:
                logger.exception("Unhandled background review worker failure")
            finally:
                with self._condition:
                    if succeeded:
                        prior_flags = self._completed.get(request.key, (0.0, 0))[1]
                        self._completed[request.key] = (
                            time.monotonic(),
                            prior_flags | request.flags,
                        )
                    self._active_key = None
                    self._active_flags = 0
                    self._condition.notify_all()


_COORDINATOR = BackgroundReviewCoordinator()


def get_background_review_coordinator() -> BackgroundReviewCoordinator:
    return _COORDINATOR


def ensure_background_review_owner_token(agent: Any) -> str:
    token = str(getattr(agent, "_background_review_owner_token", "") or "")
    if not token:
        token = uuid.uuid4().hex
        try:
            setattr(agent, "_background_review_owner_token", token)
        except Exception:
            # Slot-locked test doubles and third-party wrappers can still use
            # process-local dedupe for their lifetime.
            token = f"object-{id(agent)}-{token}"
    return token


@contextmanager
def foreground_turn_scope(agent: Any) -> Iterator[None]:
    """Track primary work so queued reviews wait until foreground is idle."""

    coordinator = get_background_review_coordinator()
    tracked = is_primary_foreground_agent(agent)
    if tracked:
        coordinator.foreground_started()
    try:
        yield
    finally:
        if tracked:
            coordinator.foreground_finished()


__all__ = [
    "BackgroundReviewCoordinator",
    "ensure_background_review_owner_token",
    "foreground_turn_scope",
    "get_background_review_coordinator",
    "review_snapshot_fingerprint",
]
