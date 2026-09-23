"""Real Discord adapter methods with SDK response objects; no live messages."""
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.discord.adapter import DiscordAdapter


@pytest.mark.asyncio
async def test_download_bytes_author_and_channel_are_read_back():
    content='검증 결과'
    item=SimpleNamespace(filename='result.json',size=2,read=AsyncMock(return_value=b'{}'))
    message=SimpleNamespace(id=9,author=SimpleNamespace(id=1),channel=SimpleNamespace(id=123),content=content,attachments=[item])
    channel=SimpleNamespace(fetch_message=AsyncMock(return_value=message))
    adapter=object.__new__(DiscordAdapter)
    adapter._client=SimpleNamespace(user=SimpleNamespace(id=1),get_channel=lambda _:channel)
    receipt=await adapter.read_lifecycle_result('123','9')
    assert receipt==dict(message_id='9',channel_id='123',content_digest=hashlib.sha256(content.encode()).hexdigest(),
                         attachments=[dict(name='result.json',bytes=2,sha256=hashlib.sha256(b'{}').hexdigest())])
    item.read.assert_awaited_once()
    item.size=3
    with pytest.raises(ValueError,match='size'):
        await adapter.read_lifecycle_result('123','9')
    message.author.id=2
    with pytest.raises(ValueError,match='author'):
        await adapter.read_lifecycle_result('123','9')
