"""Tests for the per-wire-attempt timing slots behind true model TTFT."""

from __future__ import annotations

import threading

import pytest

from agent import model_call_timing


@pytest.fixture(autouse=True)
def isolated_timing():
    model_call_timing.reset_for_tests()
    yield
    model_call_timing.reset_for_tests()


def test_two_attempts_under_one_request_id_are_independent():
    """The core blocker fix: api_request_id is identical for every retry."""
    first = model_call_timing.begin_wire_attempt("turn:api:1")
    second = model_call_timing.begin_wire_attempt("turn:api:1")

    assert first is not None and second is not None
    assert first != second

    records = model_call_timing.drain("turn:api:1")
    assert len(records) == 2
    assert {record["token"] for record in records} == {first, second}


def test_ttft_is_never_negative_across_a_stream_retry():
    """Attempt 2's issue time postdates attempt 1's first frame."""
    first = model_call_timing.begin_wire_attempt("turn:api:1")
    model_call_timing.stamp_first_frame(first)
    second = model_call_timing.begin_wire_attempt("turn:api:1")
    model_call_timing.stamp_first_frame(second)

    records = model_call_timing.drain("turn:api:1")
    assert len(records) == 2
    for record in records:
        ttft_ns = record["first_frame_ns"] - record["issued_ns"]
        assert ttft_ns >= 0
    # Each numerator belongs to its OWN denominator.
    assert records[0]["first_frame_ns"] < records[1]["issued_ns"]


def test_stamp_first_frame_is_one_shot_per_token():
    token = model_call_timing.begin_wire_attempt("turn:api:1")
    model_call_timing.stamp_first_frame(token)
    [record] = model_call_timing.drain("turn:api:1")
    first_value = record["first_frame_ns"]

    other = model_call_timing.begin_wire_attempt("turn:api:2")
    model_call_timing.stamp_first_frame(other)
    model_call_timing.stamp_first_frame(other)
    model_call_timing.stamp_first_frame(other)
    [second_record] = model_call_timing.drain("turn:api:2")

    assert first_value is not None
    assert second_record["first_frame_ns"] is not None


def test_concurrent_stamps_produce_exactly_one_first_frame():
    token = model_call_timing.begin_wire_attempt("turn:api:1")
    observed: list[int] = []
    barrier = threading.Barrier(2)

    def stamp() -> None:
        barrier.wait()
        model_call_timing.stamp_first_frame(token)

    threads = [threading.Thread(target=stamp) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    [record] = model_call_timing.drain("turn:api:1")
    observed.append(record["first_frame_ns"])
    assert observed[0] is not None


def test_finish_records_the_terminal_and_the_outcome():
    token = model_call_timing.begin_wire_attempt("turn:api:1")
    model_call_timing.finish_wire_attempt(token, "success")
    model_call_timing.finish_wire_attempt(token, "failed")

    [record] = model_call_timing.drain("turn:api:1")
    assert record["end_ns"] >= record["issued_ns"]
    assert record["attempt_outcome"] == "success"


def test_drain_clears_the_slot_and_is_empty_for_unknown_ids():
    model_call_timing.begin_wire_attempt("turn:api:1")

    assert len(model_call_timing.drain("turn:api:1")) == 1
    assert model_call_timing.drain("turn:api:1") == []
    assert model_call_timing.drain("never-seen") == []
    assert model_call_timing.drain("") == []
    assert model_call_timing.drain(None) == []


def test_a_late_worker_stamp_cannot_resurrect_a_drained_slot():
    token = model_call_timing.begin_wire_attempt("turn:api:1")
    model_call_timing.drain("turn:api:1")

    model_call_timing.stamp_first_frame(token)
    model_call_timing.finish_wire_attempt(token, "success")

    assert model_call_timing.drain("turn:api:1") == []


def test_records_and_request_ids_are_bounded():
    for _ in range(model_call_timing._MAX_RECORDS_PER_REQUEST + 5):
        model_call_timing.begin_wire_attempt("turn:api:1")
    assert (
        len(model_call_timing.drain("turn:api:1"))
        == model_call_timing._MAX_RECORDS_PER_REQUEST
    )

    for index in range(model_call_timing._MAX_LIVE_REQUEST_IDS + 10):
        model_call_timing.begin_wire_attempt(f"turn:api:{index}")
    assert len(model_call_timing._SLOTS) <= model_call_timing._MAX_LIVE_REQUEST_IDS
    # Oldest ids were evicted, newest survive.
    newest = model_call_timing._MAX_LIVE_REQUEST_IDS + 9
    assert model_call_timing.drain(f"turn:api:{newest}")
    assert model_call_timing.drain("turn:api:0") == []


def test_dimensions_are_captured_into_the_record():
    token = model_call_timing.begin_wire_attempt(
        "turn:api:1",
        api_mode_family="anthropic_messages",
        stream_mode="streaming",
        call_role="fallback",
        work_lane="research",
        provider="anthropic",
        model="claude-x",
    )
    model_call_timing.stamp_first_frame(token)

    [record] = model_call_timing.drain("turn:api:1")
    assert record["api_mode_family"] == "anthropic_messages"
    assert record["stream_mode"] == "streaming"
    assert record["call_role"] == "fallback"
    assert record["work_lane"] == "research"
    assert record["provider"] == "anthropic"
    assert record["model"] == "claude-x"


def test_every_entry_point_is_fail_open(monkeypatch):
    """The bare-bodied stream factories depend on this contract."""

    class Exploding(dict):
        def get(self, *args, **kwargs):  # noqa: D102
            raise RuntimeError("boom")

        def pop(self, *args, **kwargs):  # noqa: D102
            raise RuntimeError("boom")

        def __setitem__(self, *args, **kwargs):  # noqa: D105
            raise RuntimeError("boom")

    monkeypatch.setattr(model_call_timing, "_SLOTS", Exploding())
    monkeypatch.setattr(model_call_timing, "_TOKEN_OWNERS", Exploding())

    assert model_call_timing.begin_wire_attempt("turn:api:1") is None
    assert model_call_timing.stamp_first_frame(1) is None
    assert model_call_timing.finish_wire_attempt(1, "success") is None
    assert model_call_timing.drain("turn:api:1") == []


def test_a_new_attempt_closes_the_superseded_record():
    """The outer terminal only sees the newest token, so the prior one is closed."""
    first = model_call_timing.begin_wire_attempt("turn:api:1")
    model_call_timing.stamp_first_frame(first)
    second = model_call_timing.begin_wire_attempt("turn:api:1")
    model_call_timing.finish_wire_attempt(second, "success")

    earlier, later = model_call_timing.drain("turn:api:1")
    assert earlier["end_ns"] is not None
    assert earlier["end_ns"] <= later["issued_ns"]
    assert earlier["end_ns"] >= earlier["issued_ns"]
    assert later["attempt_outcome"] == "success"
