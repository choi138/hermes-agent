"""Corruption-focused check: run the split many times and verify NO original
character is lost or overwritten by synthetic fence markup.

Usage: <venv python> verify_corruption.py [runs]
"""
import asyncio
import re
import sys

LIMIT = 2000
SEED = "시작합니다.\n"


class FakeResult:
    def __init__(self, mid, success=True):
        self.message_id = mid
        self.success = success
        self.error = None


class FakeAdapter:
    MAX_MESSAGE_LENGTH = LIMIT
    RESEND_FINAL_ON_EMPTY_STREAM_FALLBACK = False

    def __init__(self):
        self.messages, self.order, self._n = {}, [], 0

    async def send(self, chat_id=None, content=None, reply_to=None,
                   metadata=None, **kw):
        self._n += 1
        mid = f"m{self._n}"
        self.messages[mid] = content
        self.order.append(mid)
        return FakeResult(mid)

    async def edit_message(self, chat_id=None, message_id=None, content=None,
                           finalize=False, metadata=None, **kw):
        self.messages[message_id] = content
        return FakeResult(message_id)

    async def delete_message(self, chat_id, message_id):
        self.messages.pop(message_id, None)
        if message_id in self.order:
            self.order.remove(message_id)
        return True


def build_payload():
    head = "설명 문단입니다. 조금 더 길게 씁니다.\n" * 30
    sql = [f"SELECT col_{i} FROM verification_code_consumption WHERE id = {i};"
           for i in range(45)]
    return head + "```sql\n" + "\n".join(sql) + "\n```\n" + \
        "\n마무리 문단입니다. 이어지는 설명이 있습니다.\n" * 25


async def one_run(chunk_size, delay):
    from gateway.stream_consumer import (
        GatewayStreamConsumer, StreamConsumerConfig,
    )
    payload = build_payload()
    expected = SEED + payload
    ad = FakeAdapter()
    sc = GatewayStreamConsumer(
        ad, "c1",
        StreamConsumerConfig(edit_interval=0.0, buffer_threshold=1, cursor=""),
        metadata={},
    )
    t = asyncio.create_task(sc.run())
    sc.on_delta(SEED)
    await asyncio.sleep(0.15)
    for i in range(0, len(payload), chunk_size):
        sc.on_delta(payload[i:i + chunk_size])
        await asyncio.sleep(delay)
    sc.finish()
    await asyncio.wait_for(t, timeout=60)
    msgs = [ad.messages[m] for m in ad.order if ad.messages.get(m)]
    return expected, msgs


def find_corruption(expected, msgs):
    """Every non-fence line of the original must appear verbatim somewhere."""
    delivered = "\n".join(msgs)
    lost = []
    for line in expected.splitlines():
        s = line.strip()
        if not s or s.startswith("```"):
            continue
        if line not in delivered:
            lost.append(line)
    # also: any delivered line where ``` is glued to other text
    glued = []
    for m in msgs:
        for line in m.splitlines():
            s = line.strip()
            if s.startswith("```") and len(s) > 3:
                tag = s[3:]
                # a language tag is a bare word; anything with spaces/;
                # means real content got overwritten
                if not re.fullmatch(r"[A-Za-z0-9_+-]{0,15}", tag):
                    glued.append(line)
    return lost, glued


async def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    bad = 0
    for r in range(runs):
        chunk_size = 60 + (r * 17) % 120
        delay = 0.002 + (r % 4) * 0.003
        expected, msgs = await one_run(chunk_size, delay)
        lost, glued = find_corruption(expected, msgs)
        status = "ok"
        if lost or glued:
            bad += 1
            status = "CORRUPT"
        print(f"run {r:2d}  chunk={chunk_size:3d} delay={delay:.3f}  "
              f"msgs={len(msgs)}  {status}")
        if glued:
            for g in glued[:3]:
                print(f"        glued fence -> {g[:70]!r}")
        if lost:
            for l in lost[:3]:
                print(f"        LOST line   -> {l[:70]!r}")
    print()
    print(f"RESULT: {bad}/{runs} runs corrupted")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
