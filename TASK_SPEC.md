# FOLLOW-UP: your commit f1c51134d5 introduced a DATA CORRUPTION regression

Your change fixed the fence balancing but broke content integrity. This is verified,
reproducible, and strictly worse than the code you replaced. Fix it.

## Evidence

I drove `GatewayStreamConsumer` directly with a fake Discord adapter (2000 char cap),
streaming a payload whose overflow split lands inside a ```sql block, across 12 runs
with varying delta sizes and timings:

```
BASELINE  e84df9929f (before your commit):   0/12 runs corrupted
AFTER     f1c51134d5 (your commit):         12/12 runs corrupted
```

Sample corrupted output (delivered message content):

```
'```ECT col_30 FROM verification_code_consumption WHERE id = 30;'
   ^^^ original text was 'SELECT col_30 FROM ...' — 'SEL' was OVERWRITTEN
'```id = 27;'
'```on WHERE id = 26;'
'```습니다.'
```

In every run at least one original line is destroyed and does not appear anywhere in
the delivered messages. Losing user-visible content is far worse than the rendering
bug you were asked to fix.

## Root cause — the exact line

In `gateway/stream_consumer.py`, path B, you wrote:

```python
self._accumulated = "".join(chunks[1:])
```

`self._truncate_for_stream(...)` / `split_text_fence_aware(...)` return chunks that are
each INDEPENDENTLY fence-balanced for standalone delivery. Every chunk after a split
inside a code block has a synthetic opening fence prepended AND a synthetic closing
fence appended.

Demonstrated directly:

```
original tail  : 2 fence markers
"".join(chunks[1:]) : 4 fence markers
```

So joining them re-injects ``` pairs into the middle of the buffer. That polluted
buffer then goes through the loop again, gets re-split, accumulates MORE synthetic
fences, and the fence text ends up overwriting real characters.

`chunks[1:]` is a list of PRESENTATION-READY messages. It is not a text remainder.
Joining it is not a valid way to reconstruct "what is left".

## What the fix must do

Path B must seal exactly ONE head chunk per loop iteration and leave the remaining
*source* text — not re-joined presentation chunks — in `self._accumulated`.

Requirements:

1. Compute the head chunk with the fence-aware splitter (keep that part — it is correct)
   and send it via the existing `await self._send_or_edit(chunk, finalize=True,
   is_turn_final=False)`.
2. Determine how much of the ORIGINAL `self._accumulated` that head chunk consumed,
   and set `self._accumulated` to the untouched remainder of the original string,
   plus the reopening fence needed to continue the code block.
   - Do not reconstruct the remainder by concatenating splitter output.
   - The remainder must be byte-identical to the original source text for every
     character the head chunk did not consume.
3. The reopened fence must carry the original language tag (```sql -> ```sql).
4. Preserve every constraint from the original TASK_SPEC.md: the `break` on
   `self._fallback_final_send or not ok` must leave the FULL remaining text intact,
   `is_turn_final=False` on sealed heads, no infinite loop on a degenerate budget,
   head chunk <= `_safe_limit` under `_len_fn` including any closing fence.
5. `_stream_ledger` / `delivered_final_matches` behaviour must stay correct — that
   part of your commit currently passes and must not regress.

Note: `gateway/platforms/base.py` already solves exactly this problem in
`truncate_message()` — it tracks `carry_lang` and slices the ORIGINAL `remaining`
string rather than re-joining emitted chunks. Study that loop and mirror its
structure inside path B.

## Mandatory verification

`verify_corruption.py` is in the repository root. It is the harness that found this.
Run it and paste the real output:

```
python verify_corruption.py 12
```

It must report `0/12 runs corrupted`. Anything else means you are not done.

Also run, and paste real output:

```
python verify_fence_split.py
```

This one must reach `RESULT: ALL PASS`. Its key assertion is semantic: every line of
the original must render in the SAME code-vs-prose state as the original. An even
fence count is NOT sufficient — an orphan closing fence flips prose into code while
keeping the count even.

And re-run the suite:

```
python -m pytest tests/gateway/ -x -q -k "stream or split or fence or consumer"
```

## Also fix your tests

Your added tests passed while the code corrupted data in 12/12 runs. They only checked
`sealed_head.count("```") % 2 == 0` and `tail.startswith("```sql\n")` — both of which a
corrupting implementation satisfies. Strengthen them:

- assert the concatenation of delivered chunks, after removing ONLY synthetic boundary
  fences, contains every original line verbatim
- assert no delivered line has ``` glued to non-language-tag content
  (e.g. reject '```ECT col_30 FROM ...')
- assert the code/prose render state of every original line is preserved

Commit on the current branch. Do not push. Do not open a PR.
