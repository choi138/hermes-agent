"""rvw r5x CONFIRMED (c2a9df9b): the source-line guard rejects forced
character splits.

Claim: BasePlatformAdapter.truncate_message can split MID-LINE when there is
no newline/space in budget. A partial long line is not in source.splitlines(),
so the guard bails and returns the indicator-bearing chunks. Consequences:
  1. " (1/N)" survives into the delivered message
  2. _source_tail_after_sealed_stream_chunk then cannot map -> falls back,
     leaving the oversized buffer
"""
import sys
sys.path.insert(0, ".")

from gateway.platforms.base import BasePlatformAdapter
from gateway.stream_consumer import (
    GatewayStreamConsumer, StreamConsumerConfig,
    _strip_splitter_page_indicators,
)


class Adapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = 2000
    async def connect(self): pass
    async def disconnect(self): pass
    async def get_chat_info(self, chat_id): return {}
    async def send(self, **kw): pass


print("=== forced mid-line split (no newline/space in budget) ===")
source = "```python\n" + "x" * 2000 + "\n```\n"
raw = BasePlatformAdapter.truncate_message(source, 500, len_fn=len)
print(f"  source : {len(source)} chars, {len(source.splitlines())} lines")
print(f"  chunks : {len(raw)}")
for i, c in enumerate(raw):
    last = c.splitlines()[-1] if c.splitlines() else ""
    print(f"    [{i}] last line: {last[:44]!r}")
print()

gated = _strip_splitter_page_indicators(list(raw), source)
held = gated == list(raw)
print(f"  gate result: {'HELD (indicators survive)  <<< CONFIRMED' if held else 'stripped'}")
if held:
    for i, c in enumerate(gated):
        last = c.splitlines()[-1] if c.splitlines() else ""
        if "/" in last and "(" in last:
            print(f"    chunk {i} still carries: {last[-24:]!r}")
print()

print("=== consequence 2: source-tail mapping ===")
consumer = GatewayStreamConsumer(object.__new__(Adapter), "chat",
                                 StreamConsumerConfig(cursor=""))
chunks = consumer._truncate_for_stream(source, 500, len)
head = chunks[0]
tail = consumer._source_tail_after_sealed_stream_chunk(source, head)
print(f"  head last line: {head.splitlines()[-1][:40]!r}")
print(f"  mapping -> {'None (falls back, no seal)  <<< CONFIRMED' if tail is None else 'ok'}")
print()

print("=== does this reach the real stream path? ===")
import asyncio
from gateway.platforms.base import SendResult


class Recording(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = 2000
    def __init__(self):
        self.messages, self.order, self._n = {}, [], 0
    async def connect(self): pass
    async def disconnect(self): pass
    async def get_chat_info(self, chat_id): return {}
    async def send(self, chat_id=None, content=None, reply_to=None,
                   metadata=None, **kw):
        self._n += 1; mid = f"m{self._n}"
        self.messages[mid] = content; self.order.append(mid)
        return SendResult(success=True, message_id=mid)
    async def edit_message(self, chat_id=None, message_id=None, content=None,
                           finalize=False, metadata=None, **kw):
        self.messages[message_id] = content
        return SendResult(success=True, message_id=message_id)


async def go():
    payload = "설명.\n```python\n" + "x" * 3000 + "\n```\n뒷말.\n"
    ad = Recording()
    sc = GatewayStreamConsumer(
        ad, "c1",
        StreamConsumerConfig(edit_interval=0.0, buffer_threshold=1, cursor=""),
        metadata={},
    )
    t = asyncio.create_task(sc.run())
    sc.on_delta("시작.\n")
    await asyncio.sleep(0.12)
    for i in range(0, len(payload), 150):
        sc.on_delta(payload[i:i + 150])
        await asyncio.sleep(0.003)
    sc.finish()
    await asyncio.wait_for(t, timeout=60)
    msgs = [ad.messages[m] for m in ad.order if ad.messages.get(m)]
    print(f"  delivered {len(msgs)} messages")
    bad = 0
    over = 0
    for i, m in enumerate(msgs, 1):
        if len(m) > 2000:
            over += 1
            print(f"    #{i}: {len(m)} chars  <<< OVER LIMIT")
        for line in m.splitlines():
            s = line.strip()
            if s.startswith("```") and "/" in s and "(" in s:
                bad += 1
                print(f"    #{i} fence: {line[:46]!r}  <<< INDICATOR")
    if not bad and not over:
        print("    clean")
    return bad, over

asyncio.run(go())
