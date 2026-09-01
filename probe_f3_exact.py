"""F3 exact post-fix behaviour, without shell quoting hazards."""
import sys
sys.path.insert(0, ".")

from gateway.platforms.base import BasePlatformAdapter
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


class A(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = 2000
    async def connect(self): pass
    async def disconnect(self): pass
    async def get_chat_info(self, c): return {}
    async def send(self, **k): pass


sc = GatewayStreamConsumer(object.__new__(A), "c", StreamConsumerConfig(cursor=""))

source = "````python\ncode line\n````\nprose after\n"
head = "````python\ncode line\n```"          # synthetic 3-tick close

tail = sc._source_tail_after_sealed_stream_chunk(source, head, 2)

before = "`\nprose after\n"                   # what the pre-fix code returned
print(f"pre-fix tail : {before!r}")
print(f"post-fix tail: {tail!r}")
print()

if tail is None:
    print("mapping refused -> full buffer preserved (safe, no corruption)")
else:
    checks = {
        "original 4-tick close preserved": "\n````\n" in tail,
        "prose intact": "prose after" in tail,
        "no stray LONE backtick line": not tail.startswith("`\n"),
    }
    for k, v in checks.items():
        print(f"  {k:32s}: {'OK' if v else 'FAIL'}")
    print()
    print("VERDICT:", "corruption fixed" if all(checks.values()) else "STILL BROKEN")
