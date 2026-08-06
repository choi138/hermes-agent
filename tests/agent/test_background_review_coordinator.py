from __future__ import annotations

import threading
import time

from agent.background_review_coordinator import BackgroundReviewCoordinator


def _factory(callback):
    def build(review_memory, review_skills):
        def target():
            callback(review_memory, review_skills)

        return target

    return build


def test_identical_active_request_is_deduplicated():
    coordinator = BackgroundReviewCoordinator(idle_grace_seconds=0)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = []

    def run(memory, skills):
        calls.append((memory, skills))
        started.set()
        assert release.wait(2)
        finished.set()

    first = coordinator.submit(
        owner_token="owner",
        messages_snapshot=[{"role": "user", "content": "same"}],
        review_memory=True,
        review_skills=False,
        target_factory=_factory(run),
    )
    assert first == "queued"
    assert started.wait(1)

    duplicate = coordinator.submit(
        owner_token="owner",
        messages_snapshot=[{"role": "user", "content": "same"}],
        review_memory=True,
        review_skills=False,
        target_factory=_factory(run),
    )
    assert duplicate == "deduplicated"

    release.set()
    assert finished.wait(1)
    assert calls == [(True, False)]


def test_pending_dimensions_are_coalesced_and_reviews_are_single_flight():
    coordinator = BackgroundReviewCoordinator(idle_grace_seconds=0)
    first_started = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()
    active = 0
    peak_active = 0
    calls = []
    lock = threading.Lock()

    def run(memory, skills):
        nonlocal active, peak_active
        with lock:
            active += 1
            peak_active = max(peak_active, active)
        try:
            calls.append((memory, skills))
            if len(calls) == 1:
                first_started.set()
                assert release_first.wait(2)
            else:
                second_finished.set()
        finally:
            with lock:
                active -= 1

    coordinator.submit(
        owner_token="owner",
        messages_snapshot=[{"role": "user", "content": "first"}],
        review_memory=True,
        review_skills=False,
        target_factory=_factory(run),
    )
    assert first_started.wait(1)

    queued = coordinator.submit(
        owner_token="owner",
        messages_snapshot=[{"role": "user", "content": "second"}],
        review_memory=True,
        review_skills=False,
        target_factory=_factory(run),
    )
    coalesced = coordinator.submit(
        owner_token="owner",
        messages_snapshot=[{"role": "user", "content": "second"}],
        review_memory=False,
        review_skills=True,
        target_factory=_factory(run),
    )
    assert queued == "queued"
    assert coalesced == "coalesced"

    release_first.set()
    assert second_finished.wait(1)
    assert calls == [(True, False), (True, True)]
    assert peak_active == 1


def test_review_waits_until_foreground_finishes():
    coordinator = BackgroundReviewCoordinator(idle_grace_seconds=0.02)
    ran = threading.Event()

    coordinator.foreground_started()
    coordinator.submit(
        owner_token="owner",
        messages_snapshot=[{"role": "user", "content": "work"}],
        review_memory=False,
        review_skills=True,
        target_factory=_factory(lambda _memory, _skills: ran.set()),
    )

    assert not ran.wait(0.05)
    finished_at = time.monotonic()
    coordinator.foreground_finished()
    assert ran.wait(1)
    assert time.monotonic() - finished_at >= 0.015
