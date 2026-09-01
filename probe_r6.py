"""rvw r6: two CONFIRMED findings.

F1 (0da48207, 4/5 CONFIRMED): O(N^2) -- each loop iteration re-splits the WHOLE
    remaining buffer but consumes only chunks[0].

F2 (9aba3424, 5/5 CONFIRMED): source_tail restores whitespace the base splitter
    lstrip()'d away. After leaving the overflow while (via _message_id=None) the
    tail is sent WITHOUT re-checking the limit -> oversized send.
"""
import asyncio
import sys
import time

sys.path.insert(0, ".")

from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig

LIMIT = 2000


class Recording(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = LIMIT

    def __init__(self):
        self.messages, self.order, self._n = {}, [], 0
        self.split_calls = 0

    async def connect(self): pass
    async def disconnect(self): pass
    async def get_chat_info(self, chat_id): return {}

    async def send(self, chat_id=None, content=None, reply_to=None,
                   metadata=None, **kw):
        self._n += 1
        mid = f"m{self._n}"
        self.messages[mid] = content
        self.order.append(mid)
        return SendResult(success=True, message_id=mid)

    async def edit_message(self, chat_id=None, message_id=None, content=None,
                           finalize=False, metadata=None, **kw):
        self.messages[message_id] = content
        return SendResult(success=True, message_id=message_id)


# ---------------------------------------------------------------- F2 first
print("=== F2: whitespace restored into source_tail, sent unchecked ===")


async def f2():
    # Long run of blank lines right after the split point. The base splitter
    # lstrip()s them, so its own last chunk is small -- but source_tail puts
    # them back, inflating the buffer the consumer then sends.
    head = "설명 문단입니다. 길게 채웁니다.\n" * 60
    gap = "\n" * 900                     # whitespace the splitter drops
    tail = "이어지는 본문입니다.\n" * 60
    payload = head + gap + tail

    ad = Recording()
    sc = GatewayStreamConsumer(
        ad, "c1",
        StreamConsumerConfig(edit_interval=0.0, buffer_threshold=1, cursor=""),
        metadata={},
    )
    t = asyncio.create_task(sc.run())
    sc.on_delta("시작.\n")
    await asyncio.sleep(0.12)
    for i in range(0, len(payload), 400):
        sc.on_delta(payload[i:i + 400])
        await asyncio.sleep(0.002)
    sc.finish()
    await asyncio.wait_for(t, timeout=60)

    msgs = [ad.messages[m] for m in ad.order if ad.messages.get(m)]
    over = [(i, len(m)) for i, m in enumerate(msgs, 1) if len(m) > LIMIT]
    print(f"  payload {len(payload)} chars -> {len(msgs)} messages")
    for i, m in enumerate(msgs, 1):
        mark = "  <<< OVER LIMIT" if len(m) > LIMIT else ""
        print(f"    #{i}: {len(m)} chars{mark}")
    print("  >>> CONFIRMED: oversized send" if over else "  >>> within limit")
    return bool(over)


f2_bad = asyncio.run(f2())
print()

# ---------------------------------------------------------------- F1
print("=== F1: quadratic re-splitting of the whole tail ===")


async def f1(n_chunks):
    """One huge delta -> the while loop seals ~safe_limit at a time,
    re-splitting the entire remaining buffer each pass."""
    unit = "가나다라마바사아자차카타파하 문장을 채웁니다.\n"
    payload = unit * n_chunks

    ad = Recording()
    sc = GatewayStreamConsumer(
        ad, "c1",
        StreamConsumerConfig(edit_interval=0.0, buffer_threshold=1, cursor=""),
        metadata={},
    )

    calls = {"n": 0}
    orig = sc._truncate_for_stream

    def counting(text, limit, len_fn):
        calls["n"] += 1
        calls.setdefault("chars", 0)
        calls["chars"] += len(text)
        return orig(text, limit, len_fn)

    sc._truncate_for_stream = counting

    t = asyncio.create_task(sc.run())
    sc.on_delta("시작.\n")
    await asyncio.sleep(0.12)
    sc.on_delta(payload)                 # ONE big delta
    await asyncio.sleep(0.05)
    sc.finish()
    start = time.monotonic()
    await asyncio.wait_for(t, timeout=120)
    elapsed = time.monotonic() - start
    return len(payload), calls["n"], calls.get("chars", 0), elapsed


for n in (200, 400, 800):
    size, ncalls, nchars, el = asyncio.run(f1(n))
    ratio = nchars / size if size else 0
    print(f"  payload {size:7d}  split calls {ncalls:3d}  "
          f"chars scanned {nchars:9d}  ({ratio:5.1f}x payload)  {el:.2f}s")
print()
print("  If 'chars scanned' grows ~quadratically vs payload, F1 is confirmed.")
