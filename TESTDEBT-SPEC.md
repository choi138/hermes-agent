# SPEC: 테스트 부채 정리 — 라이브 전용 temporal_mode wip와 테스트 6건 정합화

## 컨텍스트

이 worktree의 현재 브랜치 `live-current`(HEAD `4fbfd4a8d3`)는 라이브 게이트웨이와 동일한 코드다. 커밋 `bfd96bd438`("wip: preserve live-deployed recall gate/temporal/recall-log plugin state")가 라이브에서 커밋 없이 배포·운영되던 플러그인 기능(스코어 게이트 0.42, temporal_mode="current" 인자, recall-log)을 보존한 것인데, 테스트는 그 이전 동작을 기대해서 6건이 실패한다.

**방향: 코드가 정답, 테스트가 낡았다.** 이 기능들은 이미 라이브에서 운영 중이고(2026-08-28 배포, 실측 기반 게이트), 되돌리는 게 아니라 테스트를 현재 동작에 맞게 갱신한다.

## 실패 6건 (tests/plugins/memory/test_graphiti_canonical_provider.py)

1. test_model_search_tool_uses_exact_read_only_capability_and_filters_output
2. test_model_search_tool_distinguishes_empty_results_from_failures
3. test_model_search_tool_reports_filtered_candidates_and_allows_fallback
4. test_dispatch_uses_exact_bound_readonly_mcp_capability_not_registry
5. test_continuity_request_recalls_fact_through_read_only_search
6. test_preference_dependent_request_triggers_selective_recall

대표 실패 예 (6번): dispatch 호출 기대값이
`{'query': ..., 'max_facts': 24, 'group_ids': ['mnemos']}` 인데 실제는
`{'query': ..., 'max_facts': 24, 'group_ids': ['mnemos'], 'temporal_mode': 'current'}`.

## 작업

1. 6건을 하나씩 실행해 실제 diff를 확인하고, **현재 코드 동작을 기대값으로** 테스트를 갱신한다.
   - temporal_mode 인자 추가가 원인인 것은 기대 dict에 `'temporal_mode': 'current'` 추가.
   - 다른 원인(스코어 게이트, recall-log 등)이 섞여 있으면 각각 현재 동작 기준으로 수정.
   - 테스트의 **의도**(read-only capability 강제, empty/failure 구분 등)는 훼손하지 말 것. 기대값만 현행화.
2. 필요하면 temporal_mode 동작 자체를 검증하는 소규모 테스트 1개 추가(예: history-intent 질의면 temporal_mode가 빠지거나 다른 값이 되는지 — 실제 코드를 읽고 동작대로).

## 규칙

- 커밋 1개: `test(memory): align graphiti provider tests with deployed temporal/recall-log behavior`
- **push 금지. 커밋까지만.**
- 소스 코드(plugins/, agent/, gateway/) 수정 금지 — 테스트 파일만.
- targeted pytest만: `/Users/choegeun-won/.hermes/hermes-agent/venv/bin/pytest tests/plugins/memory/test_graphiti_canonical_provider.py -q`
  ```
  <pytest> tests/plugins/memory/test_graphiti_canonical_provider.py -q > /tmp/testdebt-verify.out 2>&1; echo "PYTEST_EXIT=$?" > /tmp/testdebt-verify.exit
  ```
- 완료 마커: 행 시작 위치에 `TESTDEBT_DONE_K3M9` 한 줄.

## 수용 기준

1. 커밋 1개, 지정 메시지, 테스트 파일만 변경
2. `/tmp/testdebt-verify.exit` = `PYTEST_EXIT=0` (해당 파일 전체 green)
3. 기존 통과 테스트 회귀 0
