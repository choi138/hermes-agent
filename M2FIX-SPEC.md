# SPEC: M2 교정 — ok_low_relevance가 _UNRESTRICTED_RECALL=True에서도 동작해야 함

## 배경 (스펙 결함 교정)

직전 커밋 `0b88b37066`은 M2를 구현했지만, 스펙 지시에 따라 `_UNRESTRICTED_RECALL=True`이면 강도 판정을 건너뛰고 항상 "ok"를 반환한다. 그런데 라이브 배포는 `_UNRESTRICTED_RECALL = True`(line 63)이므로 이대로면 M2가 프로덕션에서 절대 발동하지 않는다. 이 가드는 스펙 작성자의 오류였다.

원칙: `_UNRESTRICTED_RECALL`은 **개인정보 스코프 필터**(무엇을 보여줄지)를 끄는 스위치다. M2의 겹침 강도 판정은 필터가 아니라 **상태 표시**(보여준 것이 질의와 얼마나 관련 있는지)다. 따라서 unrestricted와 무관하게 항상 동작해야 한다.

## 변경 (plugins/memory/graphiti_canonical/__init__.py)

1. `_format_facts_with_count()` 내:
   - 현재: `fact_anchors = None if _UNRESTRICTED_RECALL else _anchor_tokens(fact)`
   - 변경: `fact_anchors = _anchor_tokens(fact)` — 항상 계산. `_fact_is_relevant`에 그대로 전달(그 함수 내부의 unrestricted 조기 return은 유지 — 필터링은 계속 꺼진 상태).
   - strong_overlap_count 집계 조건에서 `fact_anchors is not None` 체크는 이제 항상 참이므로 유지하든 정리하든 무방.

2. `_prefetch_before_deadline()` 상태 결정부:
   - 현재: `if not _UNRESTRICTED_RECALL and strong_overlap_count == 0:`
   - 변경: `if strong_overlap_count == 0:` — unrestricted 조건 제거. fail-open try/except는 유지.

## 테스트 갱신

- 기존 테스트 중 "unrestricted면 항상 ok"를 단언하는 케이스가 있으면 새 의미(unrestricted여도 전부 약한 겹침이면 ok_low_relevance)로 갱신.
- 추가 케이스: `_UNRESTRICTED_RECALL=True` 패치 상태에서 약한 겹침만 있는 facts → status ok_low_relevance / 강한 겹침 1개 이상 → ok.

## 규칙

- 커밋 1개: `fix(memory): apply low-relevance status detection in unrestricted recall mode`
- push 금지. 커밋까지만.
- targeted pytest만: tests/plugins/memory/test_graphiti_canonical_provider.py tests/agent/test_memory_provider.py tests/agent/test_tool_guardrails.py
  pytest 경로: `/Users/choegeun-won/.hermes/hermes-agent/venv/bin/pytest`
  ```
  <pytest> <files> -q > /tmp/m2fix-verify.out 2>&1; echo "PYTEST_EXIT=$?" > /tmp/m2fix-verify.exit
  ```
- 완료 마커: 행 시작 위치에 `M2FIX_DONE_T5W8` 한 줄.

## 수용 기준

1. 커밋 1개, 지정 메시지
2. `/tmp/m2fix-verify.exit` = `PYTEST_EXIT=0`
3. diff가 위 플러그인 파일 + 테스트 파일만 건드림
