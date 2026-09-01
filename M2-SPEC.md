# SPEC: Graphiti 인출 신뢰도 M2 — 메타기억 (ok_low_relevance 하위 상태)

## 컨텍스트

Hermes 게이트웨이 런타임. base = 이 worktree의 현재 HEAD (M1·M3 커밋 포함).

실증된 문제: Graphiti 회상이 status=ok를 반환하면 guardrail이 다른 소스(session_search 등) 접근을 차단한다(`fallback_allowed: false`). 그런데 ok인데 내용이 질의와 무관한 경우가 실재한다 — 예: "기억 시스템 개선 방법" 질문에 "Discord requires an 응답, reaction, and pin handling for message_id 1542796..." 같은 조각이 반환됨. 관련성 게이트(`_fact_is_relevant`)는 질의 앵커 토큰과 사실 앵커 토큰의 교집합 ≥1이면 통과시키므로, "discord" 같은 범용 토큰 하나만 겹쳐도 살아남는다.

M2 목표(메타기억): 시스템이 "찾긴 했는데 관련성이 약하다"를 스스로 판정해 명시하고, 그 경우 fallback을 허용한다. 정보를 숨기지 않는다 — 회상 내용은 그대로 보여주되 상태만 정직해진다.

## 대상 파일 (정확히 이 3개 + 테스트)

1. `plugins/memory/graphiti_canonical/__init__.py`
2. `agent/tool_guardrails.py`
3. `agent/memory_manager.py` (status 파싱이 있다면 — `graphiti_first_status_from_context` 근방, line ~397-472 확인)

## 요구사항

### 1. 관련성 강도 판정 (plugins/memory/graphiti_canonical/__init__.py)

`_format_facts_with_count()`는 이미 각 kept fact에 대해 `_anchor_tokens(fact)`와 `query_anchors`를 계산한다(line ~1430대의 루프). 여기서 kept fact별 **겹침 강도**를 수집한다:

- fact 하나의 겹침 강도 = `len(query_anchors & fact_anchors)` (이미 계산되는 값들의 재사용, 새 NLP 발명 금지)
- 반환값 확장: `(text, returned_count)` → `(text, returned_count, strong_overlap_count)`
  - `strong_overlap_count` = 겹침 토큰 수 ≥ 2 인 kept fact 개수
  - 하위 호환: `_format_facts()`는 기존처럼 text만 반환 유지. `_format_facts_with_count`의 기존 호출부를 전부 찾아 새 튜플 형태에 맞게 수정할 것 (호출부는 이 파일 안에만 있음 — 확인 필수)

### 2. 상태 결정 (같은 파일, `_prefetch_before_deadline` 내 ok 경로, line ~1810대)

현재:
```python
if context:
    if routing_policy == "graphiti_first":
        return context + "\n\n" + _lookup_status_block("ok", candidate_count=len(facts), ...)
```

변경: kept 사실 전부의 겹침이 약하면(= `strong_overlap_count == 0`) status를 `"ok_low_relevance"`로 내보낸다. 하나라도 강한 겹침이 있으면 기존 `"ok"` 유지.

`_lookup_status_block()`의 safe_status 집합에 `"ok_low_relevance"` 추가. `fallback_allowed`는 `"ok"`일 때만 false — 즉 `ok_low_relevance`는 `fallback_allowed: true`.

status 블록에 한 줄 추가(모델이 읽는 안내): `ok_low_relevance`일 때만
`note: recall returned facts but none share strong anchors with the query; treat as possibly irrelevant and fall back if unhelpful`

### 3. guardrail 연동 (agent/tool_guardrails.py)

- `_GRAPHITI_ROUTING_STATUSES` frozenset(line ~103)에 `"ok_low_relevance"` 추가.
- fallback 차단 로직에서 `ok_low_relevance`는 **차단하지 않는 상태**로 분류할 것. 현재 차단은 status=ok에서만 걸리는 구조인지 확인하고(`_graphiti_deny_message`, halt 로직 근방), ok만 차단하는 구조면 집합 추가만으로 충분한지 실제 코드 흐름으로 검증. 차단 분기가 "not in {empty, filtered, ...}" 식이면 거기에도 추가.

### 4. 상태 파싱 (agent/memory_manager.py)

`graphiti_first_status_from_context()`(line ~415)가 컨텍스트 텍스트에서 status를 파싱한다. `ok_low_relevance`가 여기서 유실되거나 "missing"으로 강등되지 않는지 확인하고 필요 시 수정.

## 주의

- 회상 텍스트(사실 목록)는 절대 건드리지 않는다. 숨기지도, 자르지도 않는다. 상태만 정직해진다.
- `_UNRESTRICTED_RECALL` 모드에서는 기존 동작 유지 (강도 판정을 건너뛰고 항상 ok).
- 강도 판정 기준(겹침 ≥2)은 모듈 상수 `_STRONG_OVERLAP_MIN = 2`로 빼서 튜닝 가능하게.
- fail-open: 판정 코드에서 예외가 나면 기존 "ok"로 동작 (회귀 방지).

## 테스트 (필수)

`_format_facts_with_count`/상태 결정에 대한 테스트를 기존 graphiti 플러그인 테스트 파일 위치에 추가 (tests/ 아래에서 graphiti_canonical 관련 기존 테스트 파일을 찾아 같은 곳에):

- 강한 겹침 fact 1개 이상 → status ok, fallback_allowed false
- kept 전부 겹침 1토큰 이하 → status ok_low_relevance, fallback_allowed true, note 줄 존재
- kept 0개 → 기존 filtered/empty 동작 불변
- `_lookup_status_block("ok_low_relevance")` → safe_status 유지(“error”로 강등 안 됨)
- guardrail: `set_graphiti_routing_status("ok_low_relevance")` 후 session_search가 차단되지 않음 (기존 guardrail 테스트 파일 찾아 같은 곳에)
- `_UNRESTRICTED_RECALL=True` 경로 기존 동작 확인

## 공통 규칙

- 커밋 1개: `feat(memory): expose ok_low_relevance recall status for metamemory fallback`
- **push 금지. 커밋까지만.**
- 전체 스위트 금지. targeted만:
  ```
  <pytest> <관련 테스트 파일들> -q > /tmp/m2-verify.out 2>&1; echo "PYTEST_EXIT=$?" > /tmp/m2-verify.exit
  ```
  pytest 실행 파일: 이 worktree의 .venv에는 pytest가 없다. `/Users/choegeun-won/.hermes/hermes-agent/venv/bin/pytest`를 사용할 것.
- 완료 시 로그 마지막에 행 시작 위치에서 `M2_DONE_Q9R4` 한 줄 출력.

## 수용 기준

1. 커밋 1개, 위 메시지 규약
2. `/tmp/m2-verify.exit`에 `PYTEST_EXIT=0`
3. 위 테스트 케이스 전부 존재
4. `git diff HEAD~1..HEAD --stat`에 위 3개 소스 파일 + 테스트 파일 외 무관 파일 없음
