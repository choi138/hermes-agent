"""Edge-case matrix for path B split delivery.

Covers shapes the spec did NOT explicitly name, to catch anything the fix
handles only for the happy path.

Usage: <venv python> verify_edges.py
"""
import asyncio
import re
import sys

LIMIT = 2000
SEED = "시작합니다.\n"


class FakeResult:
    def __init__(self, mid, success=True):
        self.message_id, self.success, self.error = mid, success, None


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


def render_state(chunks):
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


async def run_payload(payload, chunk_size=100, delay=0.004):
    from gateway.stream_consumer import (
        GatewayStreamConsumer, StreamConsumerConfig,
    )
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
    await asyncio.wait_for(t, timeout=90)
    msgs = [ad.messages[m] for m in ad.order if ad.messages.get(m)]
    return sc, msgs


def audit(name, expected, msgs, sc, require_split=True):
    problems = []
    if require_split and len(msgs) < 2:
        problems.append(f"did not split (msgs={len(msgs)})")

    over = [len(m) for m in msgs if len(m) > LIMIT]
    if over:
        problems.append(f"chunk over limit: {over}")

    # content integrity: every non-fence original line must survive verbatim
    joined = "\n".join(msgs)
    lost = [l for l in expected.splitlines()
            if l.strip() and not l.strip().startswith("```") and l not in joined]
    if lost:
        problems.append(f"{len(lost)} lines LOST, e.g. {lost[0][:40]!r}")

    # no fence glued to real content
    for m in msgs:
        for line in m.splitlines():
            s = line.strip()
            if s.startswith("```") and not re.fullmatch(r"```[A-Za-z0-9_+-]{0,15}", s):
                problems.append(f"glued fence {line[:44]!r}")
                break

    # unbalanced fences per chunk
    bad = [i for i, m in enumerate(msgs, 1)
           if len([x for x in m.splitlines() if x.strip().startswith("```")]) % 2]
    if bad:
        problems.append(f"unbalanced chunks {bad}")

    # render state preserved
    want, got = render_state([expected]), render_state(msgs)
    flipped = [l for l, v in want.items() if l in got and got[l] != v]
    if flipped:
        problems.append(f"{len(flipped)} lines flipped state")

    verdict = sc.delivered_final_matches(expected)
    if verdict is not True:
        problems.append(f"delivered_final_matches={verdict!r}")

    print(f"[{'PASS' if not problems else 'FAIL'}] {name}  (msgs={len(msgs)})")
    for p in problems:
        print(f"        - {p}")
    return not problems


# ---------- payload shapes ----------

def p_nested_langs():
    """Two different fenced languages, split lands in the second."""
    a = "```python\n" + "\n".join(f"x_{i} = {i}" for i in range(20)) + "\n```\n"
    mid = "사이 설명 문단입니다.\n" * 20
    b = "```sql\n" + "\n".join(
        f"SELECT col_{i} FROM t WHERE id = {i};" for i in range(45)) + "\n```\n"
    return a + mid + b + "\n끝맺음 문단.\n" * 20


def p_no_fence():
    """Plain prose, no code at all -- must still split cleanly."""
    return "아주 평범한 한국어 문단입니다. 특별한 마크업이 없습니다.\n" * 90


def p_unclosed_fence():
    """Model emitted an opening fence and never closed it."""
    return ("도입 문단입니다.\n" * 25 + "```sql\n" +
            "\n".join(f"SELECT col_{i} FROM t WHERE id = {i};" for i in range(60)))


def p_fence_at_boundary():
    """Fence marker sits very near the split point."""
    pad = "가나다라마바사아자차카타파하 문장을 채웁니다.\n"
    head = pad * 46
    return head + "```sql\nSELECT 1;\nSELECT 2;\n```\n" + pad * 60


def p_no_newlines():
    """Long code block with very few newlines -- splitter cannot use \\n."""
    body = "; ".join(f"SELECT col_{i} FROM t WHERE id = {i}" for i in range(120))
    return "도입.\n```sql\n" + body + "\n```\n마무리.\n"


def p_cjk_heavy():
    """CJK-only inside a fence (multi-byte boundary handling)."""
    lines = [f"주석_{i}: 한국어 설명이 들어간 코드 줄입니다" for i in range(120)]
    return "설명.\n```text\n" + "\n".join(lines) + "\n```\n뒷말.\n" * 10


def p_triple_split():
    """Big enough to need three or more sealed heads."""
    sql = "\n".join(
        f"SELECT col_{i} FROM verification_code_consumption WHERE id = {i};"
        for i in range(140))
    return "도입 문단.\n" * 15 + "```sql\n" + sql + "\n```\n" + "마무리.\n" * 15


async def main():
    cases = [
        ("nested languages, split in 2nd fence", p_nested_langs()),
        ("plain prose, no fences", p_no_fence()),
        ("unclosed fence from model", p_unclosed_fence()),
        ("fence marker near split boundary", p_fence_at_boundary()),
        ("code block with almost no newlines", p_no_newlines()),
        ("CJK-only fenced content", p_cjk_heavy()),
        ("very large: 3+ sealed heads", p_triple_split()),
    ]
    ok = True
    for name, payload in cases:
        expected = SEED + payload
        try:
            sc, msgs = await run_payload(payload)
        except Exception as e:
            print(f"[FAIL] {name}  -- raised {type(e).__name__}: {e}")
            ok = False
            continue
        ok &= audit(name, expected, msgs, sc)
    print()
    print("EDGE RESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
