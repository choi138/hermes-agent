# SPEC: Graphiti 인출 신뢰도 M1(쓰기 필터) + M3(세션 연속성 포인터 강화)

## 컨텍스트

Hermes 게이트웨이 런타임 저장소. 작업 기준 커밋: `52236606a0` (라이브 게이트웨이 HEAD).
이 worktree는 그 커밋에서 분기한 격리 작업 공간이다. 라이브 체크아웃이 아니므로 재시작·배포는 범위 밖.

실증된 문제 (2026-08-29, Discord #work-orders 스레드):
- 게이트웨이가 Discord 메시지마다 `[Triggering message id: `<id>` — use as ...]` 접두줄을 유저 메시지에 붙인다 (`gateway/run.py` ~21301, 의도된 설계 — 프롬프트 캐시 보호).
- 이 접두줄이 붙은 원문이 그대로 memory WAL에 기록되고(`agent/memory_manager.py` `sync_all()` → `agent/memory_journal.py:254` `append_turn()`), Graphiti에 edge로 승격되어 "message_id 15427...에 응답·핀 처리 필요" 같은 휘발성 조각이 무관한 질문의 회상 상위에 반복 노출된다.
- 세션 auto-reset 시 `gateway/session.py:1044` `build_channel_continuity_note()`가 이전 session_id 포인터만 노트로 만들어 준다. 요약은 없어서 새 세션이 백지에서 시작한다.

## 작업 1 — M1: 게이트웨이 주입 접두줄의 메모리 유입 차단

### 요구사항
`agent/memory_manager.py`의 `sync_all()` 초입에서, WAL append와 provider sync_turn에 전달되기 **전에** user_content로부터 게이트웨이 주입 접두줄을 제거하는 필터를 추가한다.

기존 선례를 정확히 따를 것: 같은 파일의 `_strip_skill_scaffolding()` (line ~657)이 스킬 스캐폴딩에 대해 동일한 문제를 이미 푼 패턴이다. 그 함수 **바로 옆에** 형제 staticmethod로 추가하고, `sync_all()`에서 `_strip_skill_scaffolding` 호출 직후에 적용한다.

### 제거 대상 접두 블록 (유저 메시지 맨 앞에서 시작하는 연속 블록만)
1. `[Triggering message id: ...]` 한 줄 (gateway/run.py ~21315에서 주입)
2. `[Replying to: "..."]` / `[Replying to your previous message: "..."]` 한 줄 (gateway/run.py ~21320에서 주입)
3. `[Routing directive: ...]` 한 줄
4. `[Note: model was just switched from ... by the model router ...]` 한 줄

**주의**:
- 접두 블록만 벗긴다. 본문 중간에 사용자가 직접 쓴 같은 모양 텍스트는 건드리지 않는다 (맨 앞 연속 매치만).
- 제거 후 빈 문자열이 되면 기존 `_strip_skill_scaffolding` 반환 규약과 동일하게 처리(None 반환 → 턴 스킵)하되, 실제로 저 접두줄만 있고 본문이 없는 메시지는 정상 흐름에서 없으므로 방어적 처리로 충분.
- assistant_content는 건드리지 않는다.
- `_scrub`(비밀 마스킹)과 별개 층이다. scrub을 대체하지 않는다.

### 테스트 (필수)
기존 memory_manager 테스트 파일 위치를 찾아 같은 파일/디렉토리에 추가:
- 접두줄 1개만 있는 경우 제거 확인
- 접두줄 여러 개 연속(triggering + replying) 제거 확인
- 본문 중간의 `[Triggering message id: ...]` 모양 텍스트는 보존 확인
- 접두줄 없는 평범한 메시지는 무변경 확인
- 스킬 스캐폴딩과 결합된 경우 둘 다 정상 동작 확인

## 작업 2 — M3: 연속성 노트에 이전 세션 마지막 대화 요약 포함

### 요구사항
`gateway/session.py:1044` `build_channel_continuity_note()`를 확장한다. 현재는 session_id 포인터만 반환한다. 여기에 이전 세션의 마지막 상태를 덧붙인다.

설계 (포인터 강화, LLM 호출 없음):
- 이전 세션(session_id=`prev`)의 세션 저장소에서 마지막 user/assistant 메시지 몇 개(최대 3턴)를 읽는다.
- 각 메시지를 한 줄로 truncate(user 200자, assistant 300자)해서 노트에 붙인다:
  ```
  [System note: This thread had an earlier Hermes session (session_id: ...) that was auto-reset. ...기존 문구 유지...
  Last exchanges before reset:
  USER: ...
  ASSISTANT: ...
  ]
  ```
- 세션 메시지 로드는 **fail-open**: 읽기 실패·비용 과다 시 기존 포인터-only 노트로 degrade. 예외가 리셋 경로를 절대 막으면 안 된다.
- 함수 docstring의 "No LLM calls" 원칙은 유지된다. "no extra API/DB lookups"는 이 확장으로 깨지므로 docstring을 정직하게 갱신할 것.
- 세션 메시지를 읽는 기존 헬퍼(session_search 도구가 쓰는 저장소 접근 코드)를 재사용한다. 새 저장소 접근 층을 만들지 않는다. `gateway/session.py` 또는 `tools/session_search_tool.py`에서 기존 로드 경로를 찾아라.
- 읽어온 내용은 메시지 원문이므로 위 M1과 같은 이유로 게이트웨이 주입 접두줄을 벗겨서 요약에 넣는다 (M1에서 만든 필터 함수를 재사용 — import 방향이 gateway→agent라 문제없는지 확인하고, 문제 있으면 로컬 복제 대신 공용 위치로 이동).

### 테스트 (필수)
- prev_session_id 있고 메시지 로드 성공 → 노트에 "Last exchanges" 포함
- 메시지 로드 실패(예외) → 기존 포인터-only 노트 반환 (fail-open)
- 조건 미충족(플랫폼 불일치 등) → None (기존 동작 회귀 없음)

## 공통 규칙
- 커밋은 두 작업을 별도 커밋으로: `fix(memory): strip gateway-injected prefix lines before memory ingest` / `feat(gateway): include last-exchange digest in channel continuity note`
- **push 금지. 커밋까지만.**
- 전체 테스트 스위트를 돌리지 마라 (integration 테스트가 외부 서비스 필요로 행 걸림). 수정한 모듈의 테스트 파일만 targeted로 실행: `pytest <해당 테스트 파일> -q`
- pytest는 파이프 없이 실행하고 exit code를 별도 파일에 기록:
  ```
  pytest <files> -q > /tmp/m1m3-verify.out 2>&1; echo "PYTEST_EXIT=$?" > /tmp/m1m3-verify.exit
  ```
- 완료 시 로그 마지막 줄에 정확히 `M1M3_DONE_X7K2` 한 줄만 출력 (행 시작 위치에서).

## 수용 기준
1. 두 커밋이 존재하고 각각 위 커밋 메시지 규약을 따른다
2. `/tmp/m1m3-verify.exit`에 `PYTEST_EXIT=0`
3. 새 테스트가 위 명시된 케이스를 모두 커버한다
4. `git diff 52236606a0..HEAD --stat`이 memory_manager.py, session.py, 테스트 파일 외 무관한 파일을 포함하지 않는다
