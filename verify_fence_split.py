"""Independent reproduction check for the Discord split-delivery defects.

Run from a worktree root with the project venv python:

    ~/Documents/hermes-agent/.venv/bin/python verify_fence_split.py

Drives GatewayStreamConsumer directly with a fake Discord-like adapter, so the
verdict does not depend on the coding agent's own tests.

Targets PATH B: a message already exists (_message_id set) and the accumulated
buffer then grows past the platform limit.

The key check is SEMANTIC, not a fence count: for every original character we
know whether it was inside a fenced code block, and we assert the delivered
chunks render it the same way.  A chunk can have an even number of fences and
still be completely wrong (an orphan closing fence flips prose into code).
"""
import asyncio
import re
import sys

LIMIT = 2000
SEED = "시작합니다.\n"


class FakeResult:
    def __init__(self, message_id, success=True):
        self.message_id = message_id
        self.success = success
        self.error = None


class FakeAdapter:
    MAX_MESSAGE_LENGTH = LIMIT
    RESEND_FINAL_ON_EMPTY_STREAM_FALLBACK = False

    def __init__(self):
        self.messages = {}
        self.order = []
        self._n = 0

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
    body = "```sql\n" + "\n".join(sql) + "\n```\n"
    tail = "\n마무리 문단입니다. 이어지는 설명이 있습니다.\n" * 25
    return head + body + tail


def code_map(text):
    """line -> True if that line renders as code (inside a fence)."""
    out = {}
    inside = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            inside = not inside
            continue          # the fence marker itself renders as nothing
        out[line] = out.get(line, inside)
    return out


def render_state(chunks):
    """line -> True if it renders as code across the delivered chunks."""
    out = {}
    for c in chunks:
        inside = False
        for line in c.splitlines():
            if line.strip().startswith("```"):
                inside = not inside
                continue
            if line not in out:
                out[line] = inside
            elif out[line] != inside:
                out[line] = "CONFLICT"
    return out


def check(label, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))
    return bool(cond)


async def main():
    from gateway.stream_consumer import (
        GatewayStreamConsumer, StreamConsumerConfig,
    )

    payload = build_payload()
    expected_full = SEED + payload      # what the user should end up seeing
    adapter = FakeAdapter()
    cfg = StreamConsumerConfig(edit_interval=0.0, buffer_threshold=1, cursor="")
    sc = GatewayStreamConsumer(adapter, "c1", cfg, metadata={})

    print(f"payload      : {len(payload)} chars (limit {LIMIT})")
    print(f"expected_full: {len(expected_full)} chars")
    print()

    task = asyncio.create_task(sc.run())
    sc.on_delta(SEED)                    # seed -> _message_id set -> PATH B
    await asyncio.sleep(0.15)
    for i in range(0, len(payload), 100):
        sc.on_delta(payload[i:i + 100])
        await asyncio.sleep(0.005)
    sc.finish()
    try:
        await asyncio.wait_for(task, timeout=60)
    except asyncio.TimeoutError:
        print("!! consumer did not finish")
        task.cancel()
        return 2

    msgs = [adapter.messages[m] for m in adapter.order if adapter.messages.get(m)]
    print(f"delivered messages: {len(msgs)}")
    for i, m in enumerate(msgs, 1):
        f = [x for x in m.splitlines() if x.strip().startswith("```")]
        print(f"  #{i}: {len(m):5d} chars  fences={len(f)}  "
              f"first={(m.splitlines() or [''])[0][:34]!r}")
    print()

    if len(msgs) < 2:
        print("!! payload did not split -- scenario not exercised")
        return 2

    ok = True
    ok &= check("no chunk exceeds the platform limit",
                all(len(m) <= LIMIT for m in msgs),
                f"max={max(len(m) for m in msgs)}")

    # ---- SEMANTIC fence check (the one that matters) ----
    want = code_map(expected_full)
    got = render_state(msgs)
    wrong = [ln for ln, v in want.items()
             if ln in got and got[ln] != v]
    ok &= check(
        "every line renders in the SAME code/prose state as the original",
        not wrong,
        f"{len(wrong)} lines flipped, e.g. {wrong[0][:44]!r}" if wrong else "",
    )

    ok &= check("every delivered chunk has balanced code fences",
                all(len([x for x in m.splitlines()
                         if x.strip().startswith("```")]) % 2 == 0 for m in msgs))

    # ---- defect 2: would the gateway re-send the whole answer on top? ----
    verdict = sc.delivered_final_matches(expected_full)
    ok &= check("delivered_final_matches(expected_full) is True "
                "(gateway would NOT duplicate)",
                verdict is True, f"got {verdict!r}")

    joined = "\n".join(msgs)
    missing = [i for i in range(45) if f"SELECT col_{i} FROM" not in joined]
    ok &= check("all 45 SQL lines survived the split",
                not missing, f"missing: {missing[:5]}" if missing else "")

    print()
    print("RESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
