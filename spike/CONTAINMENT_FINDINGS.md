# M3 association containment: strict 1-hop, degree-capped, top-K expansion

## Result

Strict 1-hop expansion reduces the first attempt's 1+2-hop volume substantially, but it is still not contained by itself. The same 31 recalled baseline edges produced **2,880 distinct current 1-hop candidate edges**, a **92.90× fan-out**, compared with 28,802 candidates and 929.10× for the earlier 1+2-hop run.

Degree capping contains volume sharply. A maximum endpoint degree of 25, with no top-K, produced 178 candidates (**5.74×**). It is the least restrictive cap-only configuration in the sweep that reaches single-digit fan-out: cap 50 produced 10.81×. If both a hub cap and top-K are required, the least restrictive tested combined configuration, cap 100 plus K=8, produced 118 candidates (**3.81×**).

No configuration is viable for the bounded live recall prefetch **as measured**, because the shared 1-hop pre-filter Cypher cost **17.85 s minimum, 20.65 s median, and 29.77 s maximum per row**. Degree caps and top-K were applied deterministically to the returned raw candidates, so every configuration in the table shares that measured pre-filter cost. A query that prunes capped endpoints before candidate materialization could be faster, but that latency was not measured here and should not be assumed.

This is a containment result, not a usefulness result. Nothing here establishes that any expanded candidate is useful.

## Safety and frozen input

- Input was only `/home/justin/.hermes/state/recall-log.snapshot-20260831.jsonl`: 126 lines, mode `0444`, SHA-256 `13aa1a26e63780f7199b6dc5317cbad353a92b346af4127b32ac1a575274d0f6`.
- The exact 18-row sample from the first attempt was reused: rows 4, 13, 18, 33, 41, 59, 92, 94, 95, 96, 98, 104, 105, 107, 111, 115, 120, and 121. Every selected row still had the recorded 1-3 baseline edge UUIDs.
- Neo4j statements start with `MATCH`, end with `RETURN`, and use only read clauses. A static guard rejects write clauses and `CALL`.
- Every candidate edge and degree edge required `group_id = "mnemos"` and `invalid_at IS NULL`; the baseline edge UUIDs were excluded from candidate counts.
- Queries ran serially. Each `.cypher` file was staged and copied into `memory-server-neo4j-1`, then executed with credentials expanded only inside the container command.
- No service was restarted, no embedding or model API was called, no Neo4j write was issued, and no production-code or live-checkout file was changed.

## Method

For each baseline edge, both endpoint nodes were anchors. The pre-filter count is the number of distinct current 1-hop edge UUIDs reachable from any anchor in that row, excluding the row's baseline UUIDs.

For each `max_endpoint_degree`, an anchor was eligible only when its current degree was less than or equal to the cap. A candidate reachable through both an excluded hub and an eligible anchor remained eligible through the latter. `none` disables the degree cap.

After capping, top-K was applied per recall row. The deterministic rank was:

1. shared eligible anchor count, descending;
2. minimum eligible endpoint degree, ascending;
3. candidate edge UUID, ascending.

Wall-clock timing starts immediately before `docker exec ... cypher-shell` and stops after result serialization returns. It excludes `.cypher` staging and `docker cp`, but includes container exec, `cypher-shell` startup, Cypher execution, and output serialization.

## Noise proxies

The earlier deterministic proxy marks a candidate related when it shares a query content word **or** the exact baseline anchor node. Every strict 1-hop candidate shares a baseline anchor by construction, so this proxy reports **0% unrelated for every row and configuration**. It is preserved for comparability but is degenerate for a 1-hop-only measurement.

The table therefore also reports the lexical-only topical-overlap sensitivity: a candidate is treated as unrelated when its fact, relation name, and endpoint names share no normalized content word with the query. The distribution is min/median/max across the 18 rows, with empty result rows assigned 0. Both measures are deterministic proxies, **not ground truth**, and neither supports a usefulness claim.

## Configuration results

The latency column is the shared pre-filter 1-hop wall time in seconds, min/median/max across rows. `Earlier noise` is 0/0/0 for every configuration for the structural reason above.

| Max endpoint degree | Top-K/row | Candidates | Fan-out | Earlier noise proxy min/med/max | Lexical-only noise proxy min/med/max | Latency s min/med/max |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | none | 81 | 2.61× | 0% / 0% / 0% | 0% / 46.97% / 100% | 17.85 / 20.65 / 29.77 |
| 10 | 2 | 27 | 0.87× | 0% / 0% / 0% | 0% / 50.00% / 100% | 17.85 / 20.65 / 29.77 |
| 10 | 3 | 36 | 1.16× | 0% / 0% / 0% | 0% / 66.67% / 100% | 17.85 / 20.65 / 29.77 |
| 10 | 5 | 51 | 1.65× | 0% / 0% / 0% | 0% / 50.00% / 100% | 17.85 / 20.65 / 29.77 |
| 10 | 8 | 69 | 2.23× | 0% / 0% / 0% | 0% / 50.00% / 100% | 17.85 / 20.65 / 29.77 |
| 25 | none | 178 | 5.74× | 0% / 0% / 0% | 0% / 70.83% / 100% | 17.85 / 20.65 / 29.77 |
| 25 | 2 | 31 | 1.00× | 0% / 0% / 0% | 0% / 75.00% / 100% | 17.85 / 20.65 / 29.77 |
| 25 | 3 | 43 | 1.39× | 0% / 0% / 0% | 0% / 66.67% / 100% | 17.85 / 20.65 / 29.77 |
| 25 | 5 | 64 | 2.06× | 0% / 0% / 0% | 0% / 50.00% / 100% | 17.85 / 20.65 / 29.77 |
| 25 | 8 | 91 | 2.94× | 0% / 0% / 0% | 0% / 50.00% / 100% | 17.85 / 20.65 / 29.77 |
| 50 | none | 335 | 10.81× | 0% / 0% / 0% | 0% / 71.81% / 100% | 17.85 / 20.65 / 29.77 |
| 50 | 2 | 31 | 1.00× | 0% / 0% / 0% | 0% / 75.00% / 100% | 17.85 / 20.65 / 29.77 |
| 50 | 3 | 44 | 1.42× | 0% / 0% / 0% | 0% / 66.67% / 100% | 17.85 / 20.65 / 29.77 |
| 50 | 5 | 68 | 2.19× | 0% / 0% / 0% | 0% / 50.00% / 100% | 17.85 / 20.65 / 29.77 |
| 50 | 8 | 101 | 3.26× | 0% / 0% / 0% | 0% / 50.00% / 100% | 17.85 / 20.65 / 29.77 |
| 100 | none | 890 | 28.71× | 0% / 0% / 0% | 0% / 71.92% / 100% | 17.85 / 20.65 / 29.77 |
| 100 | 2 | 34 | 1.10× | 0% / 0% / 0% | 0% / 75.00% / 100% | 17.85 / 20.65 / 29.77 |
| 100 | 3 | 49 | 1.58× | 0% / 0% / 0% | 0% / 66.67% / 100% | 17.85 / 20.65 / 29.77 |
| 100 | 5 | 77 | 2.48× | 0% / 0% / 0% | 0% / 53.33% / 100% | 17.85 / 20.65 / 29.77 |
| 100 | 8 | 118 | 3.81× | 0% / 0% / 0% | 0% / 56.25% / 100% | 17.85 / 20.65 / 29.77 |
| none | none | 2,880 | 92.90× | 0% / 0% / 0% | 0% / 71.92% / 100% | 17.85 / 20.65 / 29.77 |
| none | 2 | 36 | 1.16× | 0% / 0% / 0% | 0% / 100% / 100% | 17.85 / 20.65 / 29.77 |
| none | 3 | 53 | 1.71× | 0% / 0% / 0% | 0% / 66.67% / 100% | 17.85 / 20.65 / 29.77 |
| none | 5 | 85 | 2.74× | 0% / 0% / 0% | 0% / 73.33% / 100% | 17.85 / 20.65 / 29.77 |
| none | 8 | 133 | 4.29× | 0% / 0% / 0% | 0% / 64.58% / 100% | 17.85 / 20.65 / 29.77 |

## Per-row pre-filter expansion and latency

| Row | Baseline edges | Endpoint nodes | 1-hop candidates | Wall time |
| ---: | ---: | ---: | ---: | ---: |
| 4 | 3 | 6 | 51 | 26.48 s |
| 13 | 1 | 2 | 2 | 23.65 s |
| 18 | 1 | 2 | 49 | 20.01 s |
| 33 | 2 | 4 | 60 | 18.67 s |
| 41 | 3 | 4 | 12 | 17.85 s |
| 59 | 2 | 4 | 91 | 29.77 s |
| 92 | 1 | 2 | 25 | 19.44 s |
| 94 | 1 | 2 | 3 | 24.30 s |
| 95 | 3 | 5 | 723 | 21.28 s |
| 96 | 1 | 2 | 124 | 19.18 s |
| 98 | 3 | 5 | 77 | 22.98 s |
| 104 | 1 | 2 | 12 | 19.80 s |
| 105 | 1 | 2 | 9 | 19.68 s |
| 107 | 1 | 2 | 577 | 21.33 s |
| 111 | 2 | 4 | 770 | 23.66 s |
| 115 | 1 | 2 | 94 | 19.01 s |
| 120 | 3 | 6 | 113 | 21.81 s |
| 121 | 1 | 2 | 88 | 19.83 s |

The 2,880 total also exactly matches the 1-hop subset in the first attempt's independently captured raw expansion files, row for row.

## Findings

1. **One hop is not sufficient containment.** It removes about 90% of the earlier 1+2-hop volume, but 92.90× remains too large and rows 95, 107, and 111 still return 723, 577, and 770 candidates.
2. **Hub caps work on volume.** Cap 25 alone is below 10×; cap 10 alone reaches 2.61×. The sharp jump from cap 50 (10.81×) to cap 100 (28.71×) confirms that a small number of medium/high-degree endpoints dominate expansion.
3. **Top-K guarantees a small output budget.** Even without a degree cap, K=8 limits the aggregate to 4.29×. With cap 100 it is 3.81×. This says only that the returned count is bounded.
4. **Topical lexical overlap remains weak.** The least restrictive passing cap-only configuration (cap 25) has a 70.83% median lexical-only unrelated proxy; cap 100 + K=8 has 56.25%. The exact-node version of the earlier proxy cannot distinguish noise at one hop.
5. **The measured query path is not latency-viable.** A 20.65 s median cannot fit a bounded recall prefetch. Volume containment is demonstrated, but no end-to-end configuration should be called viable until endpoint pruning is pushed into the Cypher plan and its latency is re-measured under the same serial, read-only constraints.

## Raw artifacts and reproduction

- `spike/raw/containment-selected-rows.json`: frozen snapshot identity and reused sample.
- `spike/raw/containment-row-*.json`: raw endpoint degrees, candidates, generated read-only Cypher, and per-row wall time.
- `spike/raw/containment-measurements.json`: annotated candidates and per-row measurements.
- `spike/raw/containment-aggregate.json`: all cap/top-K totals, fan-outs, noise distributions, row counts, and latency distribution.

From `/home/justin/hermes-m3-association-harness`:

```sh
python3 spike/containment_harness.py
```

`--reuse-raw` recomputes all deterministic caps, rankings, and proxy summaries without querying Neo4j.
