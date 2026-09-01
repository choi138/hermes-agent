from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from plugins.mention_inbox.notion_client import (
    NOTION_API_BASE,
    NOTION_API_VERSION,
    NotionClientError,
    NotionHttpResponse,
    NotionReadClient,
    NotionResultPage,
)
from plugins.mention_inbox.notion_collector import NotionMentionCollector
from plugins.mention_inbox.notion_runtime import NotionMentionPoller
from plugins.mention_inbox.store import MentionInboxStore

TARGET_USER_ID = "notion-user-owner"
ROOT_PAGE_ID = "11111111-1111-1111-1111-111111111111"
PARENT_BLOCK_ID = "22222222-2222-2222-2222-222222222222"
CHILD_BLOCK_ID = "33333333-3333-3333-3333-333333333333"
SECOND_BLOCK_ID = "44444444-4444-4444-4444-444444444444"


class FakeTransport:
    def __init__(self, responses: list[NotionHttpResponse]):
        self.responses = list(responses)
        self.calls: list[tuple[Any, float]] = []

    def __call__(self, request, timeout: float) -> NotionHttpResponse:
        self.calls.append((request, timeout))
        return self.responses.pop(0)


def _json_response(payload: dict, *, status: int = 200, headers=None) -> NotionHttpResponse:
    return NotionHttpResponse(
        status=status,
        headers={} if headers is None else headers,
        body=json.dumps(payload).encode(),
    )


def _text(value: str) -> dict:
    return {
        "type": "text",
        "text": {"content": value},
        "plain_text": value,
    }


def _mention(user_id: str, plain_text: str = "@owner") -> dict:
    return {
        "type": "mention",
        "mention": {
            "type": "user",
            "user": {"object": "user", "id": user_id},
        },
        "plain_text": plain_text,
    }


def _comment(
    *,
    comment_id: str = "comment-1",
    discussion_id: str = "discussion-1",
    rich_text: list[dict] | None = None,
    last_edited_time: str = "2026-07-29T00:05:00.000Z",
) -> dict:
    return {
        "object": "comment",
        "id": comment_id,
        "discussion_id": discussion_id,
        "created_time": "2026-07-29T00:00:00.000Z",
        "last_edited_time": last_edited_time,
        "created_by": {"object": "user", "id": "actor-user"},
        "rich_text": rich_text
        or [_text("please review "), _mention(TARGET_USER_ID), _text(" today")],
    }


def _paragraph_block(
    *,
    block_id: str = "block-1",
    rich_text: list[dict] | None = None,
    has_children: bool = True,
    last_edited_time: str = "2026-07-29T00:06:00.000Z",
) -> dict:
    return {
        "object": "block",
        "id": block_id,
        "type": "paragraph",
        "created_by": {"object": "user", "id": "block-author"},
        "created_time": "2026-07-29T00:00:00.000Z",
        "last_edited_time": last_edited_time,
        "has_children": has_children,
        "paragraph": {
            "rich_text": rich_text
            or [_text("please check "), _mention(TARGET_USER_ID), _text(" now")]
        },
    }


def test_comment_user_mention_normalizes_to_shared_contract() -> None:
    collector = NotionMentionCollector(target_user_id=TARGET_USER_ID)

    collected = collector.normalize_comment(_comment(), root_page_id=ROOT_PAGE_ID)

    assert collected is not None
    assert collected.source_revision == "2026-07-29T00:05:00Z"
    event = collected.event
    assert event.source.platform.value == "notion"
    assert event.source.event_id == "comment:comment-1"
    assert event.actor.actor_id == "actor-user"
    assert event.target.target_id == TARGET_USER_ID
    assert event.thread.thread_id == "discussion-1"
    assert event.thread.container_id == ROOT_PAGE_ID
    assert event.requested_action.value == "reply"
    assert event.untrusted.body == "please review @owner today"
    assert event.untrusted.metadata["coverage"] == (
        "selected accessible pages / polling / best-effort"
    )
    assert event.untrusted.metadata["object_kind"] == "comment"


def test_block_type_rich_text_mention_normalizes_to_shared_contract() -> None:
    collector = NotionMentionCollector(target_user_id=TARGET_USER_ID)

    collected = collector.normalize_block(
        _paragraph_block(),
        root_page_id=ROOT_PAGE_ID,
    )

    assert collected is not None
    assert collected.source_revision == "2026-07-29T00:06:00Z"
    event = collected.event
    assert event.source.platform.value == "notion"
    assert event.source.event_id == "block:block-1"
    assert event.thread.thread_id == f"notion-page:{ROOT_PAGE_ID}"
    assert event.thread.container_id == ROOT_PAGE_ID
    assert event.requested_action.value == "acknowledge"
    assert event.untrusted.body == "please check @owner now"
    assert event.untrusted.metadata["coverage"] == (
        "selected accessible pages / polling / best-effort"
    )
    assert event.untrusted.metadata["object_kind"] == "block"


def test_client_reads_integration_owner_user_via_users_me() -> None:
    transport = FakeTransport([
        _json_response({
            "object": "user",
            "id": "integration-bot-id",
            "type": "bot",
            "bot": {
                "owner": {
                    "type": "user",
                    "user": {"object": "user", "id": TARGET_USER_ID},
                }
            },
        })
    ])
    client = NotionReadClient(token="notion-token", transport=transport)

    assert client.get_target_user_id() == TARGET_USER_ID
    request, timeout = transport.calls[0]
    assert request.full_url == f"{NOTION_API_BASE}/users/me"
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == "Bearer notion-token"
    assert request.get_header("Notion-version") == NOTION_API_VERSION
    assert timeout == 10.0


def test_client_search_pages_uses_fixed_read_query_and_cursor() -> None:
    transport = FakeTransport([
        _json_response({
            "object": "list",
            "results": [{"object": "page", "id": ROOT_PAGE_ID}],
            "next_cursor": "cursor-2",
            "has_more": True,
        })
    ])
    client = NotionReadClient(token="notion-token", transport=transport)

    page = client.search_pages(start_cursor="cursor-1", page_size=25)

    assert page.items == ({"object": "page", "id": ROOT_PAGE_ID},)
    assert page.next_cursor == "cursor-2"
    assert page.has_more is True
    request, _ = transport.calls[0]
    assert request.full_url == f"{NOTION_API_BASE}/search"
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {
        "filter": {"property": "object", "value": "page"},
        "page_size": 25,
        "sort": {"direction": "descending", "timestamp": "last_edited_time"},
        "start_cursor": "cursor-1",
    }


def test_client_lists_block_children_with_bounded_get_pagination() -> None:
    transport = FakeTransport([
        _json_response({
            "object": "list",
            "results": [_paragraph_block()],
            "next_cursor": None,
            "has_more": False,
        })
    ])
    client = NotionReadClient(token="notion-token", transport=transport)

    page = client.list_block_children(
        ROOT_PAGE_ID,
        start_cursor="cursor-1",
        page_size=50,
    )

    assert len(page.items) == 1
    request, _ = transport.calls[0]
    parsed = urlparse(request.full_url)
    assert parsed.path == f"/v1/blocks/{ROOT_PAGE_ID}/children"
    assert parse_qs(parsed.query) == {
        "page_size": ["50"],
        "start_cursor": ["cursor-1"],
    }
    assert request.get_method() == "GET"


def test_client_lists_comments_with_bounded_get_pagination() -> None:
    transport = FakeTransport([
        _json_response({
            "object": "list",
            "results": [_comment()],
            "next_cursor": "cursor-next",
            "has_more": True,
        })
    ])
    client = NotionReadClient(token="notion-token", transport=transport)

    page = client.list_comments(ROOT_PAGE_ID, page_size=20)

    assert len(page.items) == 1
    assert page.next_cursor == "cursor-next"
    request, _ = transport.calls[0]
    parsed = urlparse(request.full_url)
    assert parsed.path == "/v1/comments"
    assert parse_qs(parsed.query) == {
        "block_id": [ROOT_PAGE_ID],
        "page_size": ["20"],
    }
    assert request.get_method() == "GET"


def test_client_retrieves_validated_page_by_get() -> None:
    transport = FakeTransport([
        _json_response({"object": "page", "id": ROOT_PAGE_ID})
    ])
    client = NotionReadClient(token="notion-token", transport=transport)

    page = client.retrieve_page(ROOT_PAGE_ID)

    assert page == {"object": "page", "id": ROOT_PAGE_ID}
    request, _ = transport.calls[0]
    assert request.full_url == f"{NOTION_API_BASE}/pages/{ROOT_PAGE_ID}"
    assert request.get_method() == "GET"
    with pytest.raises(ValueError):
        client.retrieve_page("../users/me")


def test_rate_limit_error_is_retryable_without_payload_or_token_leak() -> None:
    fake_secret = "notion-fake-secret-for-redaction-test"
    transport = FakeTransport([
        NotionHttpResponse(
            status=429,
            headers={"Retry-After": "45"},
            body=json.dumps({"message": f"private payload {fake_secret}"}).encode(),
        )
    ])
    client = NotionReadClient(token=fake_secret, transport=transport)

    with pytest.raises(NotionClientError) as captured:
        client.get_target_user_id()

    error = captured.value
    assert error.category == "rate_limited"
    assert error.status == 429
    assert error.retryable is True
    assert error.retry_after_seconds == 45
    assert fake_secret not in str(error)
    assert "private payload" not in str(error)


def test_client_paces_serial_requests_to_three_per_second() -> None:
    now = [100.0]
    sleeps: list[float] = []

    def monotonic() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    transport = FakeTransport([
        _json_response({
            "object": "user",
            "type": "bot",
            "bot": {"owner": {"type": "user", "user": {"id": TARGET_USER_ID}}},
        }),
        _json_response({"object": "page", "id": ROOT_PAGE_ID}),
    ])
    client = NotionReadClient(
        token="notion-token",
        transport=transport,
        monotonic=monotonic,
        sleep=sleep,
    )

    client.get_target_user_id()
    client.retrieve_page(ROOT_PAGE_ID)

    assert sleeps == [pytest.approx(1 / 3)]


class _FakeNotionClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def get_target_user_id(self) -> str:
        return TARGET_USER_ID

    def retrieve_page(self, page_id: str) -> dict:
        self.calls.append(("page", page_id, None))
        return {"object": "page", "id": page_id}

    def list_comments(
        self,
        block_id: str,
        *,
        start_cursor: str | None = None,
        page_size: int = 100,
    ) -> NotionResultPage:
        self.calls.append(("comments", block_id, start_cursor))
        if start_cursor is None:
            return NotionResultPage(
                items=(_comment(),),
                next_cursor="comments-2",
                has_more=True,
            )
        return NotionResultPage(
            items=(
                _comment(
                    comment_id="comment-other",
                    rich_text=[_mention("different-user")],
                ),
            ),
            next_cursor=None,
            has_more=False,
        )

    def list_block_children(
        self,
        block_id: str,
        *,
        start_cursor: str | None = None,
        page_size: int = 100,
    ) -> NotionResultPage:
        self.calls.append(("children", block_id, start_cursor))
        if block_id == ROOT_PAGE_ID and start_cursor is None:
            return NotionResultPage(
                items=(
                    _paragraph_block(
                        block_id=PARENT_BLOCK_ID,
                        has_children=True,
                    ),
                ),
                next_cursor="blocks-2",
                has_more=True,
            )
        if block_id == ROOT_PAGE_ID:
            return NotionResultPage(
                items=(
                    _paragraph_block(
                        block_id=SECOND_BLOCK_ID,
                        rich_text=[_text("no mention")],
                        has_children=False,
                    ),
                ),
                next_cursor=None,
                has_more=False,
            )
        assert block_id == PARENT_BLOCK_ID
        return NotionResultPage(
            items=(
                _paragraph_block(
                    block_id=CHILD_BLOCK_ID,
                    has_children=False,
                ),
            ),
            next_cursor=None,
            has_more=False,
        )


def test_poller_recurses_paginates_and_commits_only_after_full_scan(tmp_path) -> None:
    now = datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc)
    client = _FakeNotionClient()
    store = MentionInboxStore(tmp_path / "inbox.db")
    poller = NotionMentionPoller(
        client=client,
        page_ids=(ROOT_PAGE_ID,),
        store=store,
        poll_interval_seconds=300,
        clock=lambda: now,
        max_api_pages=10,
        max_objects=20,
        max_depth=4,
    )

    first = poller.poll_once()

    assert first.status == "ok"
    assert first.roots == 1
    assert first.api_pages == 5
    assert first.fetched == 5
    assert first.selected == 3
    assert first.created == 3
    assert first.updated == 0
    assert first.skipped == 2
    assert store.get_cursor("notion.selected_pages") == "2026-07-29T01:00:00Z"
    status = store.get_collector_status("notion.selected_pages")
    assert status is not None
    assert status.status == "ok"
    assert status.consecutive_failures == 0
    assert status.next_poll_at == datetime(2026, 7, 29, 1, 5, tzinfo=timezone.utc)
    assert store.pending_delivery_count() == 3

    second = poller.poll_once()

    assert second.status == "ok"
    assert second.created == 0
    assert second.updated == 0
    assert store.pending_delivery_count() == 3


def test_poller_rate_limit_mid_scan_does_not_commit_partial_events(tmp_path) -> None:
    now = datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc)

    class RateLimitedClient(_FakeNotionClient):
        def list_block_children(
            self,
            block_id: str,
            *,
            start_cursor: str | None = None,
            page_size: int = 100,
        ) -> NotionResultPage:
            if block_id == ROOT_PAGE_ID and start_cursor == "blocks-2":
                raise NotionClientError(
                    category="rate_limited",
                    status=429,
                    retryable=True,
                    retry_after_seconds=45,
                )
            return super().list_block_children(
                block_id,
                start_cursor=start_cursor,
                page_size=page_size,
            )

    store = MentionInboxStore(tmp_path / "inbox.db")
    poller = NotionMentionPoller(
        client=RateLimitedClient(),
        page_ids=(ROOT_PAGE_ID,),
        store=store,
        clock=lambda: now,
    )

    result = poller.poll_once()

    assert result.status == "error"
    assert result.error_category == "rate_limited"
    assert result.next_poll_seconds == 45
    assert store.get_cursor("notion.selected_pages") is None
    assert store.pending_delivery_count() == 0
    status = store.get_collector_status("notion.selected_pages")
    assert status is not None
    assert status.status == "error"
    assert status.error_category == "rate_limited"
    assert status.consecutive_failures == 1
    assert status.next_poll_at == datetime(2026, 7, 29, 1, 0, 45, tzinfo=timezone.utc)


def test_collector_ignores_removed_or_non_target_mentions_and_bounds_raw_body() -> None:
    collector = NotionMentionCollector(target_user_id=TARGET_USER_ID)

    assert collector.normalize_comment(
        _comment(rich_text=[_mention("different-user")]),
        root_page_id=ROOT_PAGE_ID,
    ) is None
    assert collector.normalize_block(
        _paragraph_block(rich_text=[_text("mention removed")]),
        root_page_id=ROOT_PAGE_ID,
    ) is None

    collected = collector.normalize_comment(
        _comment(rich_text=[_text("x" * 5000), _mention(TARGET_USER_ID)]),
        root_page_id=ROOT_PAGE_ID,
    )
    assert collected is not None
    assert len(collected.event.untrusted.body) == 4000
    assert collected.event.untrusted.body.endswith("…")
