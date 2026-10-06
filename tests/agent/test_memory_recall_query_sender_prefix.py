"""Recall queries carry what the user wrote, not gateway attribution.

In a shared multi-user gateway session the gateway prefixes each inbound message with
``[<display name>] `` so the model can tell speakers apart. That prefix is not authored text:
sent verbatim as a memory recall query, the sender's name dominates keyword and entity
retrieval, and recall returns memories that merely mention the user (e.g. old chat logs)
instead of memories about what was asked. The strip lives once in MemoryManager so every
provider's prefetch and queued prefetch get the authored text.
"""

import pytest

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider


class _RecordingProvider(MemoryProvider):
    def __init__(self):
        self.prefetched = []
        self.queued = []

    @property
    def name(self) -> str:
        return "recording"

    def initialize(self, session_id: str = "", **kwargs) -> None:
        pass

    def is_available(self) -> bool:
        return True

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query, *, session_id: str = "") -> str:
        self.prefetched.append(query)
        return ""

    def queue_prefetch(self, query, *, session_id: str = "") -> None:
        self.queued.append(query)

    def sync_turn(self, user_content, assistant_content, *, session_id: str = "", messages=None) -> None:
        pass

    def get_tool_schemas(self):
        return []


def _manager():
    mgr = MemoryManager()
    provider = _RecordingProvider()
    mgr.add_provider(provider)
    return mgr, provider


def _recall_queries(text, author_name):
    mgr, provider = _manager()
    mgr.prefetch_all(text, author_name=author_name)
    mgr.queue_prefetch_all(text, author_name=author_name)
    mgr.flush_pending(timeout=5.0)
    return provider.prefetched, provider.queued


def test_sender_prefix_is_stripped_from_prefetch_and_queued_prefetch():
    prefetched, queued = _recall_queries("[최근원] 스킬 저장해줘~", "최근원")
    assert prefetched == ["스킬 저장해줘~"]
    assert queued == ["스킬 저장해줘~"]


def test_multiline_message_keeps_its_body():
    prefetched, _ = _recall_queries("[Alice] first line\nsecond line", "Alice")
    assert prefetched == ["first line\nsecond line"]


def test_slack_prefix_with_user_id_is_stripped():
    prefetched, _ = _recall_queries("[Alice | Slack user <@U123>] deploy status?", "Alice")
    assert prefetched == ["deploy status?"]


def test_display_name_is_matched_after_gateway_neutralization():
    # The gateway collapses whitespace/control characters in the name before prefixing.
    prefetched, _ = _recall_queries("[Alice Kim] hello there friend", "Alice \n  Kim")
    assert prefetched == ["hello there friend"]


def test_name_longer_than_turn_author_cap_still_matches():
    name = "N" * 230  # prefix keeps 230 chars (<= 240); turn author keeps the first 200
    prefetched, _ = _recall_queries(f"[{name}] what changed?", name[:200])
    assert prefetched == ["what changed?"]


def test_long_name_with_spaces_still_matches():
    name = ("N " * 115).strip()  # 229 chars; the turn author keeps the first 200
    prefetched, _ = _recall_queries(f"[{name}] what changed?", name[:200])
    assert prefetched == ["what changed?"]


def test_gateway_truncated_name_matches():
    from gateway.session import neutralize_untrusted_inline_text

    name = "Alice " * 60  # 360 chars: the prefix truncates to 240 with "...", the author keeps 200
    label = neutralize_untrusted_inline_text(name)
    prefetched, _ = _recall_queries(f"[{label}] what changed?", name[:200])
    assert prefetched == ["what changed?"]


@pytest.mark.parametrize("text", ["[A...] keep this", "[Al...] notes", "[Alice...] draft"])
def test_authored_ellipsis_label_is_not_a_truncated_name(text):
    prefetched, _ = _recall_queries(text, "Alice")
    assert prefetched == [text]


def test_many_brackets_on_one_line_stay_fast():
    import time

    text = "[" + "a] " * 10000 + "tail"
    started = time.perf_counter()
    prefetched, _ = _recall_queries(text, "Alice")
    assert prefetched == [text]
    assert time.perf_counter() - started < 1.0


def test_short_author_is_not_treated_as_capped():
    # "Al" is not a capped name: "[Alice]" must not match it by prefix.
    prefetched, _ = _recall_queries("[Alice] status?", "Al")
    assert prefetched == ["[Alice] status?"]


@pytest.mark.parametrize(
    "note",
    [
        '[Triggering message id: `1` - use as `message_id`]',
        '[Replying to: "line1\nline2"]',  # reply pointer keeps the quoted message whole
        "[The user sent a document: 'a.pdf'. It is saved at: /x.]",
        "[The user sent an image~ Here's what I can see:\na cat\n\non a sofa]\n"
        "[If you need a closer look, use vision_analyze with image_url: /x.png ~]",
        "[The user sent a voice message: /a.ogg (duration: 0:03)]",
    ],
)
def test_only_the_sender_token_goes_when_gateway_notes_precede_it(note):
    # Notes stay (they are context the query had before); only the attribution token goes.
    prefetched, queued = _recall_queries(f"{note}\n\n[Alice] what about this?", "Alice")
    assert prefetched == [f"{note}\n\nwhat about this?"]
    assert queued == prefetched


def test_voice_transcript_ahead_of_the_prefix_is_kept():
    # _prepend_media_prefix puts the transcript before the already-prefixed caption.
    text = '[Replying to: "lunch?"]\n\n"book the usual place for noon"\n\n[Alice] '
    prefetched, queued = _recall_queries(text, "Alice")
    assert prefetched == ['[Replying to: "lunch?"]\n\n"book the usual place for noon"']
    assert queued == prefetched


def test_history_backfill_keeps_context_and_drops_author_tokens():
    # Shape of a real Discord turn with history backfill (gateway/run_inbound.py).
    text = (
        "[Recent channel messages]\n[unverified] [Bob] any update?\n[unverified] [Alice] ping\n"
        "[Alice] handoff 작성해줘\n\n[New message]\n[Alice] 여기 스레드에서 어떤 작업 하고 있었지?"
    )
    prefetched, _ = _recall_queries(text, "Alice")
    assert prefetched == [
        "[Recent channel messages]\n[unverified] [Bob] any update?\n[unverified] ping\n"
        "handoff 작성해줘\n\n[New message]\n여기 스레드에서 어떤 작업 하고 있었지?"
    ]


def test_telegram_addressed_message_header_is_kept():
    text = (
        "[Observed Telegram group context - x]\n[Bob] hi\n\n"
        "[Current addressed message - y]\n[Alice] what did we decide about pricing"
    )
    prefetched, _ = _recall_queries(text, "Alice")
    assert prefetched == [
        "[Observed Telegram group context - x]\n[Bob] hi\n\n"
        "[Current addressed message - y]\nwhat did we decide about pricing"
    ]


def test_authored_new_message_marker_is_kept():
    text = "[Alice] summarize this transcript:\n\n[New message]\nfinal line"
    prefetched, _ = _recall_queries(text, "Alice")
    assert prefetched == ["summarize this transcript:\n\n[New message]\nfinal line"]


def test_other_speakers_are_left_alone():
    text = "[The user sent a voice message: /a.ogg]\n\n[Bob] hello"
    prefetched, _ = _recall_queries(text, "Alice")
    assert prefetched == [text]


@pytest.mark.parametrize(
    "text, author_name",
    [
        ("[WIP] fix the retrieval test", "Alice"),  # bracket text that is not the sender
        ("[Alice] status?", None),  # no author known: never guess
        ("ask [Alice] about it", "Alice"),  # not at the start of a line
        ("[Alice]no space", "Alice"),  # not the gateway's "[Name] " shape
    ],
)
def test_authored_brackets_are_preserved(text, author_name):
    prefetched, queued = _recall_queries(text, author_name)
    assert prefetched == [text]
    assert queued == [text]


def test_prefix_only_message_skips_recall():
    prefetched, queued = _recall_queries("[Alice] ", "Alice")
    assert prefetched == []
    assert queued == []


def test_default_call_without_author_is_unchanged():
    mgr, provider = _manager()
    mgr.prefetch_all("what do you remember?")
    mgr.queue_prefetch_all("next turn")
    mgr.flush_pending(timeout=5.0)
    assert provider.prefetched == ["what do you remember?"]
    assert provider.queued == ["next turn"]
