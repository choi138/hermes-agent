"""Does the surviving " (1/2)" indicator actually break rendering?

verify_original_bug.py treats any line starting with ``` as a fence toggle,
which is too generous: Markdown only CLOSES a block with a bare fence.
A fence carrying an info string ("``` (1/2)") OPENS a new block instead.

Re-check the minimal branch with correct Markdown semantics.
"""
import re
import sys

sys.path.insert(0, ".")


def render_state(text):
    """(line, is_code) using real Markdown fence rules.

    A fence line closes the block only when nothing follows the backticks.
    Anything else (```python, ``` (1/2)) opens a block with an info string.
    """
    in_code = False
    out = []
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("```"):
            rest = s[3:].strip()
            if in_code and rest == "":
                in_code = False          # genuine close
            elif in_code and rest:
                # cannot close; stays open, the line itself is code
                out.append((line, True))
            else:
                in_code = True           # open (with or without info string)
            continue
        out.append((line, in_code))
    return out


def main():
    import asyncio
    from gateway.platforms.base import BasePlatformAdapter, SendResult
    from gateway.stream_consumer import (
        GatewayStreamConsumer, StreamConsumerConfig,
    )

    LIMIT = 2000

    class Rec(BasePlatformAdapter):
        MAX_MESSAGE_LENGTH = LIMIT

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

        async def edit_message(self, chat_id=None, message_id=None,
                               content=None, finalize=False, metadata=None, **kw):
            self.messages[message_id] = content
            return SendResult(success=True, message_id=message_id)

    prose = "설명 문단입니다. 조금 더 길게 씁니다.\n" * 30
    sql = "\n".join(
        f"SELECT col_{i} FROM verification_code_consumption WHERE id = {i};"
        for i in range(45)
    )
    tail = "마무리 문단입니다. 이어지는 설명이 있습니다.\n" * 8
    payload = f"{prose}```sql\n{sql}\n```\n\n{tail}"

    async def go():
        ad = object.__new__(Rec)
        ad.messages, ad.order, ad._n = {}, [], 0
        sc = GatewayStreamConsumer(
            ad, "chat",
            StreamConsumerConfig(edit_interval=0.0, buffer_threshold=1, cursor=""),
            metadata={},
        )
        t = asyncio.create_task(sc.run())
        sc.on_delta("시작합니다.\n")
        await asyncio.sleep(0.1)
        for i in range(0, len(payload), 220):
            sc.on_delta(payload[i:i + 220])
            await asyncio.sleep(0.003)
        sc.finish()
        await asyncio.wait_for(t, timeout=60)
        return [ad.messages[m] for m in ad.order if ad.messages.get(m)]

    delivered = asyncio.run(go())

    want = dict(render_state("시작합니다.\n" + payload))
    got = {}
    for m in delivered:
        for line, state in render_state(m):
            if line.strip():
                got[line] = state

    flipped = [ln for ln, st in got.items() if ln in want and want[ln] != st]
    indicators = []
    for i, m in enumerate(delivered, 1):
        for line in m.split("\n"):
            s = line.strip()
            if s.startswith("```") and re.search(r"\(\d+/\d+\)", s):
                indicators.append((i, s))

    print(f"{len(delivered)} messages")
    print(f"  fences carrying an indicator : {indicators or 'none'}")
    print(f"  lines rendering in the wrong state : {len(flipped)}")
    for ln in flipped[:5]:
        print(f"      {ln[:60]!r}")
    print()
    print("RESULT:", "clean" if not flipped else "RENDER BREAKAGE")
    return 0 if not flipped else 1


sys.exit(main())
