"""Does the glued ' (1/N)' actually break rendering, and does the real
stream path deliver it?

1. Markdown semantics: a closing fence line must be ``` optionally followed by
   whitespace. '``` (1/4)' is an INFO STRING on an OPENING fence, so the block
   never closes.
2. Drive the real consumer with a BasePlatformAdapter-derived adapter to see
   what actually lands on screen.
"""
import asyncio
import sys
sys.path.insert(0, ".")

from gateway.platforms.base import BasePlatformAdapter
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig

LIMIT = 2000
SEED = "시작합니다.\n"


class RecordingAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = LIMIT
    RESEND_FINAL_ON_EMPTY_STREAM_FALLBACK = False

    def __init__(self):
        self.messages, self.order, self._n = {}, [], 0

    async def connect(self): pass
    async def disconnect(self): pass
    async def get_chat_info(self, chat_id): return {}

    async def send(self, chat_id=None, content=None, reply_to=None,
                   metadata=None, **kw):
        from gateway.platforms.base import SendResult
        self._n += 1
        mid = f"m{self._n}"
        self.messages[mid] = content
        self.order.append(mid)
        return SendResult(success=True, message_id=mid)

    async def edit_message(self, chat_id=None, message_id=None, content=None,
                           finalize=False, metadata=None, **kw):
        from gateway.platforms.base import SendResult
        self.messages[message_id] = content
        return SendResult(success=True, message_id=message_id)

    async def delete_message(self, chat_id, message_id):
        self.messages.pop(message_id, None)
        if message_id in self.order:
            self.order.remove(message_id)
        return True


def render_state(chunks):
    """Markdown-accurate: a fence line with a non-empty info string OPENS."""
    out = {}
    for c in chunks:
        inside = False
        for line in c.splitlines():
            s = line.strip()
            if s.startswith("```"):
                info = s[3:].strip()
                if inside and info:
                    # A fence with an info string cannot close a block.
                    continue          # stays open -- this is the bug
                inside = not inside
                continue
            out.setdefault(line, inside)
    return out


def build():
    return ("도입 문단입니다.\n" * 12
            + "```sql\n"
            + "\n".join(f"SELECT col_{i} FROM verification_code_consumption "
                        f"WHERE id = {i};" for i in range(60))
            + "\n```\n"
            + "마무리 문단입니다.\n" * 25)


async def main():
    payload = build()
    expected = SEED + payload
    ad = RecordingAdapter()
    sc = GatewayStreamConsumer(
        ad, "c1",
        StreamConsumerConfig(edit_interval=0.0, buffer_threshold=1, cursor=""),
        metadata={},
    )
    t = asyncio.create_task(sc.run())
    sc.on_delta(SEED)
    await asyncio.sleep(0.15)
    for i in range(0, len(payload), 100):
        sc.on_delta(payload[i:i + 100])
        await asyncio.sleep(0.004)
    sc.finish()
    await asyncio.wait_for(t, timeout=60)

    msgs = [ad.messages[m] for m in ad.order if ad.messages.get(m)]
    print(f"delivered {len(msgs)} messages")
    bad = []
    for i, m in enumerate(msgs, 1):
        lines = m.splitlines()
        fences = [l for l in lines if l.strip().startswith("```")]
        print(f"  #{i}: {len(m)} chars")
        for f in fences:
            s = f.strip()
            info = s[3:].strip()
            flag = ""
            if "/" in info and "(" in info:
                flag = "   <<< INDICATOR GLUED TO FENCE"
                bad.append((i, f))
            print(f"       fence: {f!r}{flag}")
    print()
    if bad:
        print(f">>> CONFIRMED on the real stream path: {len(bad)} corrupted fence(s)")
    else:
        print(">>> no glued indicator on the real stream path")

    want, got = render_state([expected]), render_state(msgs)
    flipped = [l for l, v in want.items() if l in got and got[l] != v]
    print(f"lines rendering in the wrong state: {len(flipped)}")
    if flipped:
        print("  e.g.", repr(flipped[0][:50]))
    return 1 if (bad or flipped) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
