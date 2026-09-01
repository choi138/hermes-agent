"""Reproduce the ORIGINAL reported bug: a code fence split mid-block.

Mirrors the screenshot: prose, then a long ```sql block that must be cut
across the 2000-char boundary, then more prose. The failure mode was:

  chunk 1 ended without a closing fence   -> following prose rendered as code
  chunk 2 began with a bare SELECT line   -> SQL rendered as plain text

Run from a worktree root:  python verify_original_bug.py
"""
import asyncio
import sys

sys.path.insert(0, ".")

from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig

LIMIT = 2000


class Recording(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = LIMIT

    def __init__(self):
        self.messages, self.order, self._n = {}, [], 0

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return {}

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


def build_payload():
    prose = "설명 문단입니다. 조금 더 길게 씁니다.\n" * 30
    sql = "\n".join(
        f"SELECT col_{i} FROM verification_code_consumption WHERE id = {i};"
        for i in range(45)
    )
    tail = "마무리 문단입니다. 이어지는 설명이 있습니다.\n" * 8
    return f"{prose}```sql\n{sql}\n```\n\n{tail}"


def code_state_by_line(text):
    """Map each non-fence line -> True when Markdown renders it as code."""
    state, out = False, []
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            state = not state
            continue
        out.append((line, state))
    return out


async def main():
    payload = build_payload()
    adapter = object.__new__(Recording)
    adapter.messages, adapter.order, adapter._n = {}, [], 0

    consumer = GatewayStreamConsumer(
        adapter, "chat",
        StreamConsumerConfig(edit_interval=0.0, buffer_threshold=1, cursor=""),
        metadata={},
    )
    task = asyncio.create_task(consumer.run())
    consumer.on_delta("시작합니다.\n")
    await asyncio.sleep(0.1)
    for i in range(0, len(payload), 220):
        consumer.on_delta(payload[i:i + 220])
        await asyncio.sleep(0.003)
    consumer.finish()
    await asyncio.wait_for(task, timeout=60)

    delivered = [
        adapter.messages[m] for m in adapter.order if adapter.messages.get(m)
    ]
    print(f"payload {len(payload)} chars -> {len(delivered)} messages")
    assert len(delivered) > 1, "payload did not split; test is not exercising the bug"

    # 1. every message stays within the platform cap
    oversized = [len(m) for m in delivered if len(m) > LIMIT]

    # 2. fences balance inside each message
    unbalanced = [
        i for i, m in enumerate(delivered, 1) if m.count("```") % 2
    ]

    # 3. THE REAL CHECK: does each source line render in its original state?
    want = dict(code_state_by_line("시작합니다.\n" + payload))
    got = {}
    for m in delivered:
        for line, state in code_state_by_line(m):
            if line.strip():
                got[line] = state
    flipped = [ln for ln, st in got.items() if ln in want and want[ln] != st]
    missing = [ln for ln in want if ln.strip() and ln not in got]

    for i, m in enumerate(delivered, 1):
        fences = [l for l in m.split("\n") if l.strip().startswith("```")]
        print(f"  #{i}: {len(m):5d} chars   fences={fences}")

    print()
    print(f"  oversized messages : {oversized or 'none'}")
    print(f"  unbalanced fences  : {unbalanced or 'none'}")
    print(f"  lines flipped      : {len(flipped)}")
    print(f"  lines lost         : {len(missing)}")
    for ln in flipped[:3]:
        print(f"      flipped: {ln[:56]!r}")
    for ln in missing[:3]:
        print(f"      lost   : {ln[:56]!r}")

    ok = not oversized and not unbalanced and not flipped and not missing
    print()
    print("RESULT:", "PASS — original bug is fixed" if ok else "FAIL")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
