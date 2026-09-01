"""Force the sealed HEAD (chunks[0]) to end inside a code fence, then check
whether the ' (1/N)' indicator corrupts it and whether path B mapping copes.
"""
import sys
sys.path.insert(0, ".")

from gateway.platforms.base import BasePlatformAdapter
from gateway.stream_consumer import GatewayStreamConsumer as G


class A(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = 2000
    async def connect(self): pass
    async def disconnect(self): pass
    async def get_chat_info(self, chat_id): return {}
    async def send(self, **kw): pass


def probe(label, text, limit):
    chunks = BasePlatformAdapter.truncate_message(text, limit, len_fn=len)
    if len(chunks) < 2:
        print(f"[{label}] did not split")
        return
    head = chunks[0]
    last = head.splitlines()[-1]
    glued = last.strip().startswith("```") and "/" in last
    print(f"[{label}] {len(chunks)} chunks, head last line = {last!r}")
    print(f"          head ends with a fence+indicator: {glued}")

    o = object.__new__(G)
    o.adapter = object.__new__(A)
    res = G._source_tail_after_sealed_stream_chunk(o, text, head, len(chunks))
    if res is None:
        print("          source-tail mapping -> None  (SAFE fallback, no seal)")
    else:
        first = res.splitlines()[0] if res.splitlines() else ""
        print(f"          source-tail mapping -> ok, tail starts {first!r}")
    print()
    return glued, res


# Case 1: code fence starts very early so chunk 0 ends inside it.
t1 = "intro\n```sql\n" + "\n".join(f"SELECT col_{i};" for i in range(200)) + "\n```\nouttro\n"
probe("fence-dominant", t1, 900)

# Case 2: tune the limit so the boundary lands mid-fence
for lim in (400, 500, 600, 700, 800, 1000, 1200):
    chunks = BasePlatformAdapter.truncate_message(t1, lim, len_fn=len)
    if len(chunks) < 2:
        continue
    head = chunks[0]
    last = head.splitlines()[-1]
    if last.strip().startswith("```"):
        print(f"limit={lim}: head ends {last!r}")
        o = object.__new__(G); o.adapter = object.__new__(A)
        res = G._source_tail_after_sealed_stream_chunk(o, t1, head, len(chunks))
        print(f"           mapping -> {'None (safe fallback)' if res is None else 'ok'}")
