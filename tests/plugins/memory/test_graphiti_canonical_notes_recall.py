"""Regression coverage for curated notes in Graphiti automatic recall."""

from __future__ import annotations

import time

from agent.notes_store import NotesStore
from plugins.memory import graphiti_canonical as graphiti_module
from plugins.memory.graphiti_canonical import GraphitiCanonicalMemoryProvider


def _create_screenshot_note(monkeypatch, tmp_path) -> NotesStore:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = NotesStore()
    store.create(
        "incident",
        "hermes.repeat.learning.gap",
        "스크린샷을 저장하지 않아 같은 실패를 다시 만들었다.",
        evidence=["episode:" + "a" * 32],
        origin="curator",
        status="unconfirmed",
    )
    return store


def _prefetch(provider: GraphitiCanonicalMemoryProvider, query: str) -> str:
    return provider._prefetch_before_deadline(
        query, session_id="notes-recall", deadline=time.monotonic() + 10
    )


def test_notes_recall_terms_normalize_particles_deduplicate_and_cap():
    terms = graphiti_module._notes_recall_terms("스크린샷을 스크린샷을 a .")

    assert "스크린샷을" in terms
    assert "스크린샷" in terms
    assert terms.count("스크린샷을") == 1
    assert "a" not in terms

    capped = graphiti_module._notes_recall_terms(
        " ".join(f"term{index}" for index in range(30))
    )
    assert len(capped) == 24
    assert capped == [f"term{index}" for index in range(24)]


def test_format_notes_block_skips_demoted_and_truncates_gists():
    assert graphiti_module._format_notes_block([]) == ""

    block = graphiti_module._format_notes_block([
        {
            "kind": "incident",
            "topic_key": "kept.note",
            "status": "unconfirmed",
            "confidence": "supported",
            "body": "x" * (graphiti_module._NOTES_GIST_CHARS + 1),
        },
        {
            "kind": "incident",
            "topic_key": "demoted.note",
            "status": "demoted",
            "confidence": "supported",
            "body": "must not appear",
        },
    ])

    assert "# Notes Recall (curated, read-only)" in block
    assert "kept.note" in block
    assert "status=unconfirmed" in block
    assert "demoted.note" not in block
    assert "x" * (graphiti_module._NOTES_GIST_CHARS - 1) + "…" in block


def test_provider_injects_matching_note_and_records_retrieval(monkeypatch, tmp_path):
    store = _create_screenshot_note(monkeypatch, tmp_path)
    provider = GraphitiCanonicalMemoryProvider()
    provider.initialize("notes-recall", hermes_home=str(tmp_path))
    monkeypatch.setattr(provider, "_bounded_search", lambda *_args, **_kwargs: [])

    matched = _prefetch(provider, "스크린샷을 다시 확인해")
    unrelated = _prefetch(provider, "완전히 다른 일정 정리해")

    assert "# Notes Recall (curated, read-only)" in matched
    assert "incident/hermes.repeat.learning.gap" in matched
    assert "# Notes Recall (curated, read-only)" not in unrelated
    assert store.read("incident", "hermes.repeat.learning.gap")["usage"]["search_hits"] == 1


def test_correction_turn_suppresses_graphiti_but_keeps_notes(monkeypatch, tmp_path):
    _create_screenshot_note(monkeypatch, tmp_path)
    provider = GraphitiCanonicalMemoryProvider()
    provider.initialize("notes-recall", hermes_home=str(tmp_path))

    def _graphiti_must_not_run(*_args, **_kwargs):
        raise AssertionError("Graphiti must remain suppressed for corrections")

    monkeypatch.setattr(provider, "_bounded_search", _graphiti_must_not_run)

    result = _prefetch(provider, "그거 말고 스크린샷을 다시 해봐")

    assert "# Notes Recall (curated, read-only)" in result
    assert "# Graphiti Lookup Status" not in result


def test_graphiti_search_failure_keeps_notes_context(monkeypatch, tmp_path):
    _create_screenshot_note(monkeypatch, tmp_path)
    provider = GraphitiCanonicalMemoryProvider()
    provider.initialize("notes-recall", hermes_home=str(tmp_path))

    def _search_fails(*_args, **_kwargs):
        raise RuntimeError("synthetic Graphiti failure")

    monkeypatch.setattr(provider, "_bounded_search", _search_fails)

    result = _prefetch(provider, "스크린샷을 다시 확인해")

    assert "# Graphiti Lookup Status" in result
    assert "status: error" in result
    assert "# Notes Recall (curated, read-only)" in result
