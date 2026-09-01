"""Reproduce the 4 CONFIRMED findings from the minimal-branch review.

F1 ca90b4ee: path-B sealed chunk still carries " (i/N)" glued to the synthetic
    closing fence. Question: is it the LAST line (cosmetic) or mid-message?
F2 ef669d50: a normalizing splitter makes source-tail mapping fail -> break ->
    the oversized buffer goes to _send_or_edit as-is.
F3 ebc5cfbe: a synthetic "\n```" close is a PREFIX of an original 4-backtick
    close, so the generic prefix branch eats 3 of 4 backticks.
F4 9bede4bd: fallback prefix comparison vs synthetic closes -> duplicate tail.
    (complex; probed separately if time allows)
"""
import asyncio
import re
import sys

sys.path.insert(0, ".")

from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig

LIMIT = 2000


def recorder(splitter=None):
    class Ad(BasePlatformAdapter):
        MAX_MESSAGE_LENGTH = LIMIT

        def __init__(self):
            self.messages, self.order, self.wire, self._n = {}, [], [], 0

        async def connect(self): pass
        async def disconnect(self): pass
        async def get_chat_info(self, chat_id): return {}

        async def send(self, chat_id=None, content=None, reply_to=None,
                       metadata=None, **kw):
            self.wire.append(("send", len(content or "")))
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
    ad = object.__new__(Ad)
    ad.messages, ad.order, ad.wire, ad._n = {}, [], [], 0
    return ad


async def drive(ad, payload, *, delta=220, head="시작합니다.\n"):
    sc = GatewayStreamConsumer(
        ad, "chat",
        StreamConsumerConfig(edit_interval=0.0, buffer_threshold=1, cursor=""),
        metadata={},
    )
    t = asyncio.create_task(sc.run())
    sc.on_delta(head)
    await asyncio.sleep(0.1)
    for i in range(0, len(payload), delta):
        sc.on_delta(payload[i:i + delta])
        await asyncio.sleep(0.003)
    sc.finish()
    await asyncio.wait_for(t, timeout=60)
    return [ad.messages[m] for m in ad.order if ad.messages.get(m)]


print("=== F1 ca90b4ee: indicator on the sealed chunk (path B) ===")
prose = "설명 문단입니다. 조금 더 길게 씁니다.\n" * 30
sql = "\n".join(
    f"SELECT col_{i} FROM verification_code_consumption WHERE id = {i};"
    for i in range(45)
)
payload = f"{prose}```sql\n{sql}\n```\n\n" + "마무리 문단입니다.\n" * 8

msgs = asyncio.run(drive(recorder(), payload))
mid_message = last_line = 0
for i, m in enumerate(msgs, 1):
    lines = m.split("\n")
    for j, line in enumerate(lines):
        if line.strip().startswith("```") and re.search(r"\(\d+/\d+\)", line):
            where = "LAST-LINE" if j == len(lines) - 1 else f"MID(line {j}/{len(lines)})"
            print(f"  msg#{i}: {line.strip()!r}  [{where}]")
            if j == len(lines) - 1:
                last_line += 1
            else:
                mid_message += 1
print(f"  -> mid-message: {mid_message}, last-line: {last_line}")
print("  cosmetic only" if mid_message == 0 else "  >>> BREAKS RENDERING")
print()

print("=== F2 ef669d50: normalizing splitter -> oversized edit ===")


def normalizing(content, max_length=LIMIT, len_fn=None):
    atoms = [a for a in re.split(r"\n\s*\n", content) if a.strip()]
    out, cur = [], []
    for a in atoms:
        if cur and len("\n\n".join(cur + [a])) > max_length - 40:
            out.append("\n\n".join(cur))
            cur = [a]
        else:
            cur.append(a)
    if cur:
        out.append("\n\n".join(cur))
    return out or [content]


para = "본문 문단입니다. 충분히 길게 채웁니다.\n" * 12
payload2 = (para + "\n\n\n\n") * 8
ad2 = recorder(splitter=normalizing)
msgs2 = asyncio.run(drive(ad2, payload2, delta=400))
over_wire = [(k, n) for k, n in ad2.wire if n > LIMIT]
over_final = [len(m) for m in msgs2 if len(m) > LIMIT]
print(f"  wire over-limit: {over_wire[:4] or 'none'}")
print(f"  final over-limit: {over_final or 'none'}")
print("  >>> CONFIRMED" if over_wire or over_final else "  not reproduced")
print()

print("=== F3 ebc5cfbe: 4-backtick close eaten by the prefix branch ===")
sc = GatewayStreamConsumer(
    recorder(), "chat", StreamConsumerConfig(cursor=""), metadata={},
)
# source whose code block closes with FOUR backticks
source = "````python\ncode line\n````\nprose after\n"
# a sealed head whose synthetic close "\n```" happens to be a prefix of the
# original "\n````"
head_chunk = "````python\ncode line\n```"
tail = sc._source_tail_after_sealed_stream_chunk(source, head_chunk, 2)
print(f"  source          : {source!r}")
print(f"  head (synthetic): {head_chunk!r}")
print(f"  mapped tail     : {tail!r}")
if tail is not None and tail.startswith("`"):
    print("  >>> CONFIRMED: stray backtick(s) left at tail start")
elif tail is None:
    print("  mapping refused (None) — safe")
else:
    print("  tail clean")
