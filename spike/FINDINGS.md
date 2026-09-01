# M3 association measurement: 1–2 hop neighbor expansion

## Result

Unconditional 1–2 hop expansion is not suitable for the recall path. Across 18 varied historical recall rows, 31 logged baseline edges expanded to 28,802 distinct current neighbor edges: a **929.10× fan-out**. The per-row conservative noise-ratio estimate was **25.0% minimum, 87.95% median, and 99.10% maximum**.

Association is still worth a constrained prototype. A few graph neighborhoods recover useful context with no query-word overlap, but expansion needs a small candidate budget, hub suppression, relevance reranking, and a 1-hop-first policy. Two-hop traversal should be exceptional and gated by intermediate-node degree or confidence.

## Safety and scope

- The live checkout `/home/justin/hermes-main-runtime-reintegration-20260803` was read only and was not modified.
- The harness ran only `MATCH`/`OPTIONAL MATCH`/`WHERE`/`WITH`/`UNWIND`/`RETURN` queries. A static guard rejects Cypher write clauses.
- It queried only `group_id = "mnemos"`; expansion edges additionally require `invalid_at IS NULL`.
- Queries were copied to the host and then into the Neo4j container. Credentials expand only inside the container invocation and are never placed in a query or local shell history.
- No services were restarted and no Neo4j data was changed.
- The isolated branch/worktree is based on the requested commit `e9add2fabf81a848593f4d0614d1391b7db7a5b2`.

Two observed-state differences are recorded for reproducibility: the live checkout's current HEAD was `c4e167cd656d3f991f572837e4734eb5b9aff095` rather than the supplied base, and the cleaned recall log contained 122 rows rather than 121. The worktree nevertheless uses the exact requested base, and selection uses explicit one-based row numbers.

## Method

The original logged edge UUIDs are `baseline_kept` and are treated as signal as requested. For the two endpoint nodes of each baseline edge, the harness collects all distinct current edges first seen within one or two hops, excluding the baseline UUIDs themselves.

The deterministic primary heuristic marks a candidate related if any of these hold:

1. a Korean or English content word overlaps the normalized query;
2. it is a parallel edge between the same baseline endpoints; or
3. it is a 1-hop edge sharing a baseline anchor node (the requested “exact same node” rule).

`noise_ratio_estimate = unrelated candidates / all expanded candidates`. This is deliberately conservative: a person or project hub can share the exact node while its other facts are plainly irrelevant. As a sensitivity check, lexical overlap alone gives a higher noise distribution of **45.95% / 96.36% / 100%** (min/median/max).

## Selected historical rows

Rows with zero edges or more than three edges were excluded. The fixed sample spans project status, planning, auth, finance, social history, deployment, short continuations, and sparse-context queries rather than taking adjacent near-duplicates.

| Row | Query (shortened only for display) | Baselines | Selection reason |
| ---: | --- | ---: | --- |
| 4 | `claude-lb 서브에이전트 세팅 작업 완료` | 3 | Concise project-completion query |
| 13 | `#projects ... project post created` | 1 | Discord channel-management topic |
| 18 | `claude-lb 프로젝트 M1 마일스톤 구현 범위` | 1 | Milestone/planning topic |
| 33 | `Anthropic Team seat ... API key ... 발급 방식` | 2 | Account/authentication topic |
| 41 | `Sharadar ... Nasdaq Data Link ... 결제` | 3 | Finance/data-vendor topic |
| 59 | `m1 계획서 G0에 적어둔거 삭제해줘` | 2 | Narrow document-edit intent |
| 92 | `최근 완료한 작업 테스트 부채 Codex 위임` | 1 | Delegation/status topic |
| 94 | `승인한 1번 2번 3번 ... memory plugin cherry-pick` | 1 | Memory-plugin deployment topic |
| 95 | `memory-health-watch ... 수정 작업 완료` | 3 | Cron/heartbeat topic |
| 96 | `Codex 위임 ... 테스트 파일만 ... 실패 6건 해소` | 1 | Constrained validation topic |
| 98 | `M3 재현 Gate auto-reset ...` | 3 | Reproduction/milestone topic |
| 104 | `graphiti 원본 레포 upstream PR 제출` | 1 | Upstream-contribution topic |
| 105 | `2번이 뭐였더라?` | 1 | Sparse continuity query |
| 107 | `어 그렇게 작업 진행해줘` | 1 | Contextual continuation |
| 111 | `어 그렇게 해줘` | 2 | Ambiguous continuation stress case |
| 115 | `GD ... graphiti를 언제 불러오지?` | 1 | Minimal-vocabulary stress case |
| 120 | `인스타그램 릴스 시청 기록` | 3 | Social-history topic |
| 121 | `릴스 시청 내역도 graphiti에 남기려면...` | 1 | Ingestion/design topic |

The full original queries, timestamps, edge UUIDs, and reasons are in `raw/selected_rows.json`.

## Per-row measurements

| Row | Baseline | Expanded | Noise estimate |
| ---: | ---: | ---: | ---: |
| 4 | 3 | 978 | 94.48% |
| 13 | 1 | 6 | 66.67% |
| 18 | 1 | 972 | 93.31% |
| 33 | 2 | 1,359 | 87.49% |
| 41 | 3 | 656 | 94.66% |
| 59 | 2 | 1,566 | 93.61% |
| 92 | 1 | 139 | 74.82% |
| 94 | 1 | 4 | 25.00% |
| 95 | 3 | 4,840 | 84.88% |
| 96 | 1 | 1,376 | 80.09% |
| 98 | 3 | 1,457 | 92.59% |
| 104 | 1 | 74 | 45.95% |
| 105 | 1 | 1,004 | 99.10% |
| 107 | 1 | 4,095 | 85.91% |
| 111 | 2 | 5,340 | 85.58% |
| 115 | 1 | 1,883 | 94.95% |
| 120 | 3 | 1,174 | 88.42% |
| 121 | 1 | 1,879 | 94.41% |

## Useful association cases

These candidates are plausibly useful even though the heuristic found no exact query-word overlap (or the useful relation is graph context rather than keyword repetition):

- **Row 94 (strongest):** the approved memory-plugin/cherry-pick query reaches the 2-hop fact `5fd77f3e3의 Graphiti recall denylist 변경이 hermes/all-work에 반영됐다.` (`aa0315e6-4c37-4056-b2d7-4d24304c0a7e`). It supplies concrete integration status for the same verified cherry-pick neighborhood. This small neighborhood had only four candidates and 25% estimated noise.
- **Row 4:** the claude-lb subagent-setup completion query reaches ``config.yaml`의 channel_prompts 2곳은 Claude Code 위임을 허용하도록 반영되었다.` (`91bfeee4-ab43-451a-a9a7-33bddccf11ce`). “Delegation enabled in channel prompts” supports the setup-completion question despite different vocabulary.
- **Row 104:** the Graphiti upstream-PR query reaches `apps/graphiti-explorer/ should not keep cache/*.snapshot.json in source form.` (`0c9a2472-8699-4d7e-9d7b-4603fcbdae89`). This is plausible repository/PR hygiene context that direct query overlap would miss.

## Noise cases

- **Row 120 (strongest structural counterexample):** `인스타그램 릴스 시청 기록` reaches the 1-hop fact `User_32465743853 says Strava is the app they use for running.` (`00d05323-17df-466b-944e-c81d27f27d36`), along with unrelated movie, travel, shoe, and conversational facts. The broad exact-node rule labels this related solely because it shares a DM-participant node. Person hubs make even 1-hop expansion unsafe without filtering.
- **Row 92:** the completed-work/test-debt query reaches `Dash board 조사_Task mentions Propella as a COPD DTx example.` (`0f6883e2-df37-4b7a-8ac5-32ecaa214ac6`) and similar Akili/Prispira/Daylight facts. A task node is acting as a topic hub, not evidence for the user's status question.
- **Row 13:** the Discord project-post query has only six candidates, but a 2-hop candidate is Gmail subject `Re: [silviahealth/sona] eng-10679-add-insight-api (PR #38)` (`917db532-e8c0-4097-b485-7ee7360bf69c`). Four of the six are unrelated PR-email metadata.
- **Row 105:** the extremely sparse query `2번이 뭐였더라?` expands one baseline to 1,004 candidates with **99.10%** estimated noise, showing how contextual queries amplify hub traversal.

## Recommendation

Proceed only with a gated experiment, not a production recall-path change:

- begin with 1-hop candidates only;
- enforce a small per-anchor and total top-k budget;
- suppress or degree-cap generic, person, session, task, and project hubs;
- rerank against the query and discard low-relevance candidates;
- allow 2-hop traversal only through low-degree or explicitly high-confidence intermediates;
- evaluate relation/node-type allowlists and keep the existing noise filters after expansion.

Row 94 demonstrates real association value, but the 929× aggregate fan-out and 87.95% median conservative noise make naive expansion decisively non-viable.

## Reproduce

From the isolated worktree:

```sh
python3 spike/association_harness.py \
  --host choi138-ri \
  --recall-log /home/justin/.hermes/state/recall-log.jsonl \
  --output-dir spike/raw
```

Use `--reuse-raw` to recompute selection, flags, and aggregates from the captured per-row expansion JSON without querying Neo4j again. `raw/measurements.json` contains every candidate and flag; `raw/aggregate.json` is the compact aggregate.

For the standalone helper mode, repeat `--edge-uuid UUID` for each requested baseline edge. It writes baseline details and distinct 1–2 hop current candidates to `spike/raw/edge-probe.json` using the same read-only path.
