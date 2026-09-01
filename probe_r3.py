"""rvw r3 finding: does _strip_page_indicator eat GENUINE user content?

Claim: the strip runs on every adapter's chunks, including non-base/legacy
splitters that never add indicators. If a real reply legitimately ends with
"... (1/3)", that text is silently deleted.
"""
import sys
sys.path.insert(0, ".")

from gateway.stream_consumer import (
    GatewayStreamConsumer, StreamConsumerConfig, _strip_page_indicator,
)

print("=== 1. helper on genuine trailing content ===")
cases = [
    "우리 팀 승률은 (1/3)",
    "Split the work (1/3)",
    "ratio (2/5)",
    "```\ncode\n```\nfinal ratio (1/2)",
]
for c in cases:
    out = _strip_page_indicator(c)
    flag = "  <<< CONTENT LOST" if out != c else ""
    print(f"  {c!r}\n    -> {out!r}{flag}")
print()

print("=== 2. via a NON-BASE adapter (no indicators ever added) ===")


class LegacyAdapter:
    """Not a BasePlatformAdapter. Custom splitter, 2-arg legacy shape."""
    MAX_MESSAGE_LENGTH = 2000

    @staticmethod
    def truncate_message(content, max_length):
        # naive split; never appends "(i/N)"
        return [content[i:i + max_length] for i in range(0, len(content), max_length)]


text = "A" * 1500 + "\n마지막 줄 비율은 (1/3)"
consumer = GatewayStreamConsumer(LegacyAdapter(), "chat",
                                 StreamConsumerConfig(cursor=""))
chunks = consumer._truncate_for_stream(text, 800, len)
print(f"  input ends with : {text[-20:]!r}")
print(f"  chunks          : {len(chunks)}")
print(f"  last chunk ends : {chunks[-1][-20:]!r}")
lost = not chunks[-1].endswith("(1/3)")
print("  >>> CONFIRMED: genuine content deleted" if lost else "  >>> preserved")
print()

print("=== 3. via a BASE adapter, single chunk (no indicator added) ===")
from gateway.platforms.base import BasePlatformAdapter


class BaseLike(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = 2000
    async def connect(self): pass
    async def disconnect(self): pass
    async def get_chat_info(self, chat_id): return {}
    async def send(self, **kw): pass


short = "짧은 답변, 비율은 (1/3)"
c2 = GatewayStreamConsumer(object.__new__(BaseLike), "chat",
                           StreamConsumerConfig(cursor=""))
out = c2._truncate_for_stream(short, 800, len)
print(f"  input      : {short!r}")
print(f"  chunks     : {len(out)}")
print(f"  result     : {out[0]!r}")
print("  >>> CONFIRMED: content deleted on a NON-split payload"
      if out[0] != short else "  >>> preserved")
