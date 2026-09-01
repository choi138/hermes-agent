"""Reproduce rvw r2 finding: does the sealed head chunk keep a ' (1/N)' indicator
glued after a synthetic closing fence?

The claim: BasePlatformAdapter.truncate_message appends " (i/N)" to EVERY chunk
when len(chunks) > 1. Path B sends chunks[0] as the sealed edit. If the split
fell inside a code fence, chunks[0] ends with "\n```" and the indicator lands
AFTER it -> "\n``` (1/2)", which is no longer a valid closing fence.
"""
import sys
sys.path.insert(0, ".")

from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.bluebubbles import BlueBubblesAdapter


def show(label, chunks):
    print(f"--- {label}: {len(chunks)} chunks ---")
    for i, c in enumerate(chunks):
        last = c.splitlines()[-1] if c.splitlines() else ""
        print(f"  [{i}] ends with: {last!r}")
    print()


text = (
    "prose line\n" * 30
    + "```sql\n"
    + "\n".join(f"SELECT col_{i} FROM t WHERE id = {i};" for i in range(60))
    + "\n```\n"
    + "trailing prose\n" * 20
)

print(f"payload {len(text)} chars\n")

base_chunks = BasePlatformAdapter.truncate_message(text, 900, len_fn=len)
show("BasePlatformAdapter", base_chunks)

head = base_chunks[0]
tail_line = head.splitlines()[-1]
print("SEALED HEAD last line :", repr(tail_line))
print("ends with '\\n```'     :", head.endswith("\n```"))
print("has (i/N) indicator   :", tail_line.strip().startswith("```") and "(" in tail_line)
print()

if tail_line.strip().startswith("```") and "/" in tail_line:
    print(">>> CONFIRMED: indicator glued to the closing fence")
    print(">>> that line is NOT a valid closing fence for Markdown")
else:
    print(">>> not reproduced with this shape")
print()

bb_chunks = BlueBubblesAdapter.truncate_message(text, 900, len_fn=len)
show("BlueBubbles (strips indicators)", bb_chunks)

# Now: what does _source_tail_after_sealed_stream_chunk do with a head that
# carries the indicator? It only strips " (1/N)" for BasePlatformAdapter
# instances -- check the mapping still succeeds.
from gateway.stream_consumer import GatewayStreamConsumer as G


class A(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = 2000
    async def connect(self): pass
    async def disconnect(self): pass
    async def get_chat_info(self, chat_id): return {}
    async def send(self, **kw): pass


o = object.__new__(G)
o.adapter = object.__new__(A)
res = G._source_tail_after_sealed_stream_chunk(o, text, head, len(base_chunks))
print("source-tail mapping with indicator head ->",
      "None (falls back)" if res is None else repr(res[:50]))
