"""rvw r7: three CONFIRMED findings.

F1 (8745dc6e, 5/5): after the strike cap fires, _fallback_final_send is set but
    the done path still sends the unsplit buffer; mid-stream ticks keep pushing
    the over-limit buffer through _send_or_edit because path A is now skipped.

F2 (1d5379cb, 4/5): a splitter that NORMALIZES text (Yuanbao collapses blank-line
    runs) produces a head that is not a source prefix -> mapping returns None ->
    break -> the oversized original buffer goes out in one message.

F3 (4a37051c, 5/5): base splitter drops the boundary newline from its own
    continuation (remaining[split_at:].lstrip()), but our source-tail mapping
    returns the untouched suffix, so the next message starts with a blank line.
"""
import asyncio
import sys
sys.path.insert(0, ".")

from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig

LIMIT = 2000


def recorder(*, send_ok=True, splitter=None):
    class Ad(BasePlatformAdapter):
        MAX_MESSAGE_LENGTH = LIMIT

        def __init__(self):
            self.messages, self.order, self._n = {}, [], 0
            self.wire = []

        async def connect(self): pass
        async def disconnect(self): pass
        async def get_chat_info(self, chat_id): return {}

        async def send(self, chat_id=None, content=None, reply_to=None,
                       metadata=None, **kw):
            self.wire.append(("send", len(content or "")))
            if not send_ok:
                return SendResult(success=False, error="rejected")
            self._n += 1
            mid = f"m{self._n}"
            self.messages[mid] = content
            self.order.append(mid)
            return SendResult(success=True, message_id=mid)

        async def edit_message(self, chat_id=None, message_id=None,
                               content=None, finalize=False, metadata=None, **kw):
            self.wire.append(("edit", len(content or "")))
            self.messages[message_id] = content
            return SendResult(success=True, message_id=message_id)

    if splitter is not None:
        Ad.truncate_message = staticmethod(splitter)
    return Ad


async def drive(ad, payload, *, delta=400, timeout=40):
    sc = GatewayStreamConsumer(
        ad, "c1",
        StreamConsumerConfig(edit_interval=0.0, buffer_threshold=1, cursor=""),
        metadata={},
    )
    t = asyncio.create_task(sc.run())
    sc.on_delta("시작.\n")
    await asyncio.sleep(0.1)
    for i in range(0, len(payload), delta):
        sc.on_delta(payload[i:i + delta])
        await asyncio.sleep(0.002)
    sc.finish()
    await asyncio.wait_for(t, timeout=timeout)
    return sc


# ------------------------------------------------------------------ F1
print("=== F1: after the strike cap, do we keep sending over-limit? ===")


async def f1():
    ad = recorder(send_ok=False)()
    payload = "본문 문단입니다. 충분히 길게 채웁니다.\n" * 400
    await drive(ad, payload)
    over = [(k, n) for k, n in ad.wire if n > LIMIT]
    print(f"  wire calls total : {len(ad.wire)}")
    print(f"  over-limit calls : {len(over)}")
    if over:
        print(f"    e.g. {over[:4]}")
        print("  >>> CONFIRMED: repeated over-limit requests")
    else:
        print("  >>> no over-limit request")

asyncio.run(f1())
print()

# ------------------------------------------------------------------ F3
print("=== F3: does the next message start with a blank line? ===")


async def f3():
    ad = recorder()()
    # plain prose, no fences: forces an ordinary newline split
    payload = "본문 문단입니다. 충분히 길게 채웁니다.\n" * 240
    await drive(ad, payload)
    msgs = [ad.messages[m] for m in ad.order if ad.messages.get(m)]
    print(f"  delivered {len(msgs)} messages")
    bad = 0
    for i, m in enumerate(msgs, 1):
        if m.startswith("\n") or m.startswith(" \n"):
            bad += 1
            print(f"    #{i} starts with {m[:14]!r}  <<< LEADING BLANK")
    if not bad:
        print("  >>> no leading blank line")
    else:
        print("  >>> CONFIRMED")

asyncio.run(f3())
print()

# ------------------------------------------------------------------ F2
print("=== F2: normalizing splitter (blank-run collapse) ===")


def normalizing(content, max_length=LIMIT, len_fn=None):
    """Mimic Yuanbao: split on blank lines, rejoin atoms with exactly \n\n."""
    import re
    atoms = [a for a in re.split(r"\n\s*\n", content) if a.strip()]
    out, cur = [], []
    for a in atoms:
        trial = "\n\n".join(cur + [a])
        if cur and len(trial) > max_length - 40:
            out.append("\n\n".join(cur))
            cur = [a]
        else:
            cur.append(a)
    if cur:
        out.append("\n\n".join(cur))
    return out or [content]


async def f2():
    ad = recorder(splitter=normalizing)()
    # three or more blank lines between paragraphs -> normalization changes text
    para = "본문 문단입니다. 충분히 길게 채웁니다.\n" * 12
    payload = (para + "\n\n\n\n") * 8
    await drive(ad, payload)
    msgs = [ad.messages[m] for m in ad.order if ad.messages.get(m)]
    over = [len(m) for m in msgs if len(m) > LIMIT]
    wire_over = [n for _, n in ad.wire if n > LIMIT]
    print(f"  payload {len(payload)} -> {len(msgs)} messages, "
          f"sizes {[len(m) for m in msgs][:6]}")
    if over or wire_over:
        print(f"  >>> CONFIRMED: over-limit final={over} wire={wire_over[:4]}")
    else:
        print("  >>> within limit")

asyncio.run(f2())
