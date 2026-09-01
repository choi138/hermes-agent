"""rvw r4: two claims against the page-indicator gate.

F1 (line 85): `[ \t]*` also eats the ORIGINAL whitespace before the marker.
     Markdown hard line break = two trailing spaces. If a split boundary line
     ends with "  " and the splitter appends " (1/2)", stripping "[ \t]*\(..\)"
     removes the hard break too.

F2 (line 1533): a NON-base custom splitter can still pass every gate check.
     Source "A (1/2)\nB (2/2)" split into ["A (1/2)", "B (2/2)"] satisfies
     positional numbering AND the source-line check -> user text deleted.
"""
import sys
sys.path.insert(0, ".")

from gateway.stream_consumer import (
    GatewayStreamConsumer, StreamConsumerConfig,
    _strip_page_indicator, _strip_splitter_page_indicators,
)

print("=== F1: hard line break (two trailing spaces) ===")
# Markdown: "line  " (2 spaces) = hard break. Splitter appends " (1/2)".
original_line = "설명 문장입니다.  "          # user's hard break
with_marker = original_line + "(1/2)"        # splitter glued the marker
out = _strip_page_indicator(with_marker)
print(f"  original : {original_line!r}")
print(f"  +marker  : {with_marker!r}")
print(f"  stripped : {out!r}")
lost = out != original_line
print("  >>> CONFIRMED: hard break destroyed" if lost else "  >>> preserved")
print()

# And the realistic shape: base appends " (i/N)" with ONE leading space.
line2 = "설명 문장입니다.  "                  # ends with 2 spaces
chunk2 = line2 + " (1/2)"                    # base adds " (1/2)"
out2 = _strip_page_indicator(chunk2)
print(f"  base-shape: {chunk2!r}")
print(f"  stripped  : {out2!r}")
print(f"  expected  : {line2!r}")
print("  >>> CONFIRMED: lost {} trailing space(s)".format(
    len(line2) - len(out2)) if out2 != line2 else "  >>> preserved")
print()

print("=== F2: non-base custom splitter with genuine ratios ===")


class EvilLegacyAdapter:
    """Not a BasePlatformAdapter. Its splitter returns the user's own text
    which happens to be positionally-numbered ratios."""
    MAX_MESSAGE_LENGTH = 2000

    @staticmethod
    def truncate_message(content, max_length):
        return content.split("\n")


source = "A (1/2)\nB (2/2)"
consumer = GatewayStreamConsumer(EvilLegacyAdapter(), "chat",
                                 StreamConsumerConfig(cursor=""))
chunks = consumer._truncate_for_stream(source, 800, len)
print(f"  source : {source!r}")
print(f"  chunks : {chunks!r}")
deleted = chunks != ["A (1/2)", "B (2/2)"]
print("  >>> CONFIRMED: user ratios deleted" if deleted else "  >>> preserved")
print()

print("  direct gate call:")
direct = _strip_splitter_page_indicators(["A (1/2)", "B (2/2)"], source)
print(f"    -> {direct!r}")
print("    >>> gate passed a NON-base result"
      if direct != ["A (1/2)", "B (2/2)"] else "    >>> gate held")
