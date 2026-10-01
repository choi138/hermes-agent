# Dispatch-shaped latency: stacked gates, queue position, and offset attribution

Applies when a batch runtime (review lanes, workers, shards, delegated agents) is slow and
the per-item durations are already known. Complements
`adversarial-verification-gating/references/root-cause-attribution-from-run-telemetry.md`,
which covers reading censored durations and excluding a load story. This file covers what
comes next: sizing concurrency, and attributing failures that correlate with *when* an item
was dispatched rather than *what* it contained.

## Enumerate every concurrency gate before tuning one

A batch runtime frequently has more than one limit, and they compose multiplicatively at the
narrowest one. Raising the one you found first just moves the wall.

Observed: a review tool held two independent limits acquired in the *same* statement.

```python
# dispatch.py
async with semaphore, host_slot(host_gate):
    raw_result = await runtime.execute(**kwargs)
```

| Gate | Value | Mechanism | Scope |
|---|---|---|---|
| `DEFAULT_CONCURRENCY` | 8 | `asyncio.Semaphore` | one command |
| `DEFAULT_HOST_CONCURRENCY` | 12 | `flock` on slot files | whole host, env-overridable |

Measured `peak concurrent = 8` proved only that the *first* gate bound. Raising it to 18
would have re-bound at 12, and the "18 → 30 min" projection would have silently failed.

Grep for the whole family before writing the change:

```sh
grep -rn 'CONCURRENCY\|MAX_.*=\s*[0-9]\|LIMIT\|semaphore\|Semaphore\|flock' src/ --include=*.py
```

Then check each candidate for an env override and a documented disable value (`0` meaning
"gate off" is common). Change all binding gates in one edit, and add a test asserting their
*relationship* — not just each value — so a future edit to one cannot re-create the hidden
bottleneck:

```python
def test_host_cap_does_not_undercut_command_concurrency():
    assert DEFAULT_HOST_CONCURRENCY >= MAX_AUTO_CONCURRENCY
```

## Size concurrency by simulation, not by guess

Per-item wall times are enough to compute the achievable floor exactly. Schedule them
longest-processing-time-first across `k` workers:

```python
def simulate(walls, k):
    import heapq
    heap = [0.0] * k
    heapq.heapify(heap)
    for w in sorted(walls, reverse=True):
        t = heapq.heappop(heap)
        heapq.heappush(heap, t + w)
    return max(heap)
```

Observed, from 18 measured runs (680 … 1800 s):

```
concurrency= 8 -> 3551s = 59.2min   (current)
concurrency=12 -> 2643s = 44.1min
concurrency=18 -> 1800s = 30.0min   <- floor reached
concurrency=24 -> 1800s = 30.0min   (no further gain)
```

The floor is `max(walls)` — the single slowest item. Two consequences worth stating to the
user: the useful ceiling is the *planned item count* (beyond it, nothing improves), and no
concurrency change can beat the slowest item, so if that item is the problem it needs a
different lever entirely.

Prefer `min(planned_items, HARD_CAP)` over a fixed default so small batches do not spawn a
crowd and large batches are still bounded. Keep an explicit user override, and make sure
"user passed the flag" is distinguishable from "default applied" (a Typer/argparse `None`
default resolved after parsing) — otherwise auto-sizing silently overrides an explicit
request.

## Correlate failure with dispatch offset, not with content

When concurrency is below the item count, items dispatch in waves. Late waves run in a
different environment than early ones (provider congestion, cache eviction, thermal, noisy
neighbours), so **failures cluster by queue position while looking like they cluster by
whatever label those late items happen to share.**

Reconstruct dispatch offsets and sort by them:

```python
t0 = os.path.getmtime(f"{run}/target.json")            # or the run's recorded start
started = os.path.getmtime(os.path.join(item_dir, "prompt.md"))
offset = int(started - t0)
```

Observed:

```
offset <= 1200s : 11 items, 0 failures
offset >  1200s :  7 items, 2 failures
```

The decisive check is a **same-content pair at different offsets**:

| offset | prompt | result |
|---|---|---|
| 0 s | 65 KB | 735 s success |
| 1318 s | 66 KB | 1800 s timeout |
| 735 s | 11 KB | 1071 s success |
| 2549 s | 11 KB | 1800 s timeout |

An 11 KB item — the lightest in the batch — died at offset 2549 s and succeeded at 735 s.
Content is excluded; position is not.

Also compute the group means before blaming a group. Here the failing category
(`adversarial-edge`, 1336 s mean) was the **fastest** of the three, ranking below
`correctness-data-flow` (1399 s) and `contract-integration` (1440 s). A category cannot be
"too heavy" while also being the quickest.

This matters because it changes the fix: raising the per-item deadline again would not help
(the 11 KB item had 1800 s and still died), while removing the late waves removes the
failures. **Two symptoms, one cause** — say so explicitly rather than proposing a separate
remedy for each, and drop any content-shaped remedy (prompt caps, exploration budgets) once
position is established.

## Record dispatch start in telemetry

Reconstructing offsets above required `stat`-ing `prompt.md` mtimes, because the usage
artifact stored only `wall_seconds`. Duration alone cannot distinguish "slow" from
"dispatched into a bad window".

Add the spawn timestamp to whatever per-item usage record already exists, at the point the
child process is launched (the same place the duration clock starts). Make the field optional
with a default so artifacts written before the change still parse, and verify that
explicitly — a schema change that breaks reading last week's runs destroys the baseline you
need for comparison.

## Pitfalls

1. **Tuning the first limit you find.** Measured peak concurrency identifies the *binding*
   gate, not the only one.
2. **Projecting a speedup from one gate's value.** The projection is only valid after every
   binding gate is raised together.
3. **Proposing a content-shaped fix for a position-shaped failure.** Exploration caps and
   prompt trimming do nothing when the lightest item dies purely from late dispatch.
4. **Blaming the category the late items belong to.** Compare category means; the failing
   one is often the fastest.
5. **Raising concurrency past the item count.** Beyond `planned_items` the wall clock is
   pinned at `max(walls)`; further parallelism only adds host load.
6. **Auto-sizing over an explicit user flag.** Resolve "unset" separately from "set to the
   default value".
