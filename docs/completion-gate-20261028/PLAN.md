# Hermes 완료 판정 게이트 적용 계획 — Hermes 전용

작성일: 2026-10-28 Asia/Seoul

목적은 사용자 요구의 필수 검증이 남아 있는데도 Hermes가 전체 작업을 완료 처리하는 경로를 닫는 것이다. 2026-10-28 사용자의 ‘FIDO는 다른 곳에서 수정하므로 Hermes만 범위를 좁혀 계획을 다시 작성’ 지시에 따라 개정했다. 이번 문서는 적용 계획이며, 운영 코드·설정·DB를 변경하지 않는다. 별도 리뷰 판정과 개정 이력은 REVIEW.md에 보관한다.

## 이번 개정의 범위

1. 포함: Hermes gateway, 계약·검증 registry, process follow-up, Hermes가 소유한 Mac 위임 launcher/runner, 공식 완료 응답과 외부 상태 전달 경계.
2. 제외: `Documents/fido-dashboard/**`와 FIDO 소유의 plugin·fidod·API·UI 수정, FIDO 명세·테스트 변경, FIDO 브라우저 QA와 배포. plugin이 Hermes 안에서 로드되더라도 FIDO 소유 파일은 변경하지 않는다.
3. 외부 연결: Hermes가 내보내는 실행 결과와 작업 완료 결정을 구분하고, 기존 확장 지점에서 검증된 완료 결정만 내보내도록 한다. FIDO 측 수신·저장·화면 반영은 별도 작업이다.
4. 이번 Hermes 작업의 합격은 Hermes 출구까지 실제 통합 시험으로 증명한다. FIDO 수정이나 브라우저 QA를 이 작업의 완료 조건으로 넣지 않으며 FIDO 결함 해결을 주장하지 않는다. 기존 FIDO 작업에 남아 있는 필수 검사는 임의로 삭제하거나 통과 처리하지 않는다.
5. 새 서비스·외부 전달 시스템·병렬 완료 DB를 만들지 않는다. 기존 lifecycle과 확장 지점을 재사용하고, 실제 연결에 필요한 참조·증거·호환 정보만 추가한다.

## 판단과 근거

기존 해결안인 ‘원래 요청을 재검토하고, 필수 요구가 미검증이면 완료를 막는다’는 방향은 유지한다. 그러나 이를 새 프레임워크로 만드는 방안은 폐기한다. 실제 실행 중인 Hermes와 현재 Mac runner에는 이미 `agent/task_lifecycle`의 계약·증거·검증 코드가 있다. 이번 FIDO 세션은 이 선택적 경로를 사용하지 않았다.

확인된 사실:

1. `TaskContract`에는 원래 요청, 목표, 요청 revision, 필수 acceptance checks가 있다. intake는 원래 요청과 필수 검사를 executor 입력에 결합한다.
2. `AcceptanceGate`는 필수 검사 누락·실패·다른 artifact revision을 거부한다. `verification.verify()`는 실제 검사 실행, 로그 hash, 독립 리뷰 verdict, 산출물 snapshot을 확인한다.
3. 이 흐름은 `run_codex_task.py lifecycle ...`일 때만 선택된다. 일반 `--spec ...` 실행은 다른 흐름이다. 일반 Discord 실행 경로에서 lifecycle intake로 연결하는 호출은 확인되지 않았고, koharu DB에 lifecycle 테이블도 없었다. 현재 `hermes-delegate`의 `gateway_accepts()`도 기존 Codex 실행 계약에서 `lifecycle` subcommand를 배제한다. 이는 해당 입구의 미연결을 보여 주며 모든 profile의 미사용까지 증명하지는 않는다.
4. 2026-10-27 문제 세션의 실제 위임은 Mac runner의 일반 `--spec` 경로였다. CI watcher의 `verified`는 `process_followups`에 저장됐으며 전체 작업의 요구 충족을 증명하는 결과는 아니다.
5. FIDO 명세는 background 종료 감지를 제외했고, 테스트와 화면 QA는 리뷰 시작 fixture로 완료를 만들었다. 원래 요구와 명세의 의미상 일치, 실제 실행 경계의 검증은 코드만으로 자동 보장되지 않는다.

확인 범위의 한계: lifecycle 모듈의 존재와 소스 동작, 실제 세션의 경로는 확인했지만 기존 lifecycle의 운영 통합·내구성 전체를 이번에 실행 시험한 것은 아니다. 아래 단계는 이 연결부터 증명한다. 컨텍스트 압축이 이번 결함의 직접 원인이었다고 단정하지 않는다.

## 적용 대상과 소유권

| 영역 | 변경 책임 | 재사용 지점 |
| --- | --- | --- |
| Hermes gateway | 원래 사용자 요청의 신뢰 가능한 식별, parent 작업 연결, 완료 상태 출력 | `gateway/run_inbound.py`, `gateway/run_turn*.py`, 기존 deterministic 실행 정책 |
| Mac 위임 입구 | 실제 Codex·Claude·QA 경로를 계약과 연결 | `hermes-delegate`, 배포된 `scripts/run_codex_task.py`, review-gate |
| 계약·검증 저장 | 필수 검사, evidence, 요청 변경, 재시작 복원 | `agent/task_lifecycle/{contract,intake,registry,verification,acceptance}.py` |
| 프로세스 결과 | worker·CI watcher 종료와 parent 작업 완료의 분리 | `tools/process_registry_followups.py`, `gateway/process_followups.py` |
| Hermes 외부 전달 경계 | 실행 결과와 검증된 작업 완료를 구분해 전달 | 기존 gateway 응답·hook/event 확장 지점. FIDO 소유 consumer는 수정하지 않음 |

수정은 저장소 정본의 격리 worktree에서 수행하고 배포 도구로 설치한다. 실행 중인 릴리스, 다른 작업의 worktree, 기존 운영 DB를 직접 패치하지 않는다. 오래된 로컬 Hermes 체크아웃을 현재 런타임 정본으로 간주하지 않는다.

## 불변 조건

1. 프로세스 종료, 검사 통과, 작업 요구 충족, 메시지 전송은 다른 사실이다. CI watcher PASS로 parent task가 완료되지 않는다. 실행 실패·취소도 검증 성공으로 치환하지 않는다.
2. 원래 요청과 필수 조건은 대화 요약과 별개로 영속 보관한다. 최초 계약과 범위 변경마다 독립 coverage verdict를 해당 contract digest에 결합한 필수 영수증으로 남긴다. 사용자 필수 결과를 삭제·완화하려면 인증된 사용자 메시지나 기존의 명시적 범위 결정이 필요하다. 모델이 자체 제외 사유와 새 revision을 만드는 것으로는 허용하지 않는다. 구현 방식 변경에는 이 이유만으로 새 사용자 승인을 요구하지 않는다.
3. 증거는 parent/child 식별자, contract digest, 산출물·검사 revision, 환경과 귀속을 확인한다. 과거 PASS, 다른 저장소·실행·배포본의 PASS는 대체할 수 없다. 검사 대상은 선언 파일뿐 아니라 실제 diff의 관련 변경을 포함하는지 리뷰한다.
4. 미충족·검증 불가·예산 소진·압축·재시작·검증기 오류는 성공이 아니다. 대화는 계속할 수 있지만 공식 작업 상태는 미완료 또는 검증 불가로 남는다. 자동 재실행이나 무한 검증 루프로 해결하지 않는다.
5. 자연어의 의미와 테스트 충분성은 모델 판단이 남는다. 기계적 검사는 증거의 존재·출처·동일성·필수 항목 충족을 보장하고, 별도 리뷰는 원래 요청과 검증 경계의 충분성을 판정한다. 거짓말 방지의 완전 보장을 주장하지 않는다.

## 1단계 현재 경로와 실패 재현 고정

예상: 0.5 작업일. 다음 단계 착수 조건은 실제 입구에서 게이트 우회가 재현되는 것이다.

1. 실행 중인 gateway의 source path, release 식별자, profile과 Mac `hermes-delegate`의 runner 경로를 manifest로 고정한다. 10월 27일 사건 당시 runner와 현재 runner를 구분한다.
2. Codex·Claude·QA·직접 편집·CI watcher·일반 질문의 호출 경로와 계약 생성 여부를 표로 만든다. 기존 lifecycle 테스트와 신뢰 경계를 읽고 재사용 범위를 확정한다.
3. 격리 profile/DB와 FIDO와 무관한 작은 저장소에서 실제 gateway 입구를 사용하는 회귀 시나리오를 만든다. 실제 산출물의 필수 동작 검사를 실행하지 않았는데 하위 unit 검사와 CI watcher는 PASS인 경우, 현재 전체 완료 경로가 열리는지 확인한다. UI나 FIDO를 사용하지 않는다.
4. 기존 lifecycle 검증이 필수 검사 누락을 거절하는 정상 대조군도 실행한다. ‘게이트 자체 결함’과 ‘입구가 게이트를 우회함’을 분리한다.
5. 수정 전 실패 증거, 테스트 이름, 실행 명령·결과·경로를 보관한다. 자연어 완료 문구의 정확한 문자열에만 의존하지 않고 parent 상태와 acceptance 영수증의 부재를 검증한다.

## 2단계 기존 계약을 실제 입구에 연결

예상: 1~2 작업일. 다음 단계 착수 조건은 gateway→Mac 실제 경로에 계약 ID와 요청 revision이 전달되는 것이다.

1. 기존 deterministic 작업 라우팅에서 코딩·수정 작업을 선택할 때 신뢰 가능한 원래 메시지와 owner/profile/repo를 계약 intake에 연결한다. 모델이 임의 owner·승인·검사 argv를 주입할 수 없게 기존 trusted grant 경계를 유지한다. `gateway_accepts()`에서 문자열 허용만 넓히지 않고 authenticated gateway envelope→Mac grant import를 실제 연결한다. synthetic completion 알림에서 사용자 권한을 재구성하지 않는다.
2. 원래 요구별 acceptance ID, 관찰 가능한 성공 조건, 관련 spec, 필수 test/review/boundary 검사를 기록한다. 같은 목표의 관련 이전 요청도 출처 메시지와 함께 연결한다. 별도 read-only 리뷰어가 이 요청들과 Non-goals를 비교하고 필수 요구 누락을 거절한다. 최초 계약과 변경 계약의 coverage 영수증에는 request revision·contract digest·검토한 원문 출처·판정·누락 항목·리뷰 실행 식별자를 묶는다. trusted aggregator는 해당 영수증을 필수로 요구한다. 사용자 필수 결과 삭제·완화는 인증된 사용자 범위 결정이 없으면 거부한다. 판단할 수 없으면 검증 불가로 남긴다.
3. 현재 Codex·Claude·QA 입구가 같은 계약 식별자를 사용하게 한다. 분류기뿐 아니라 실제 coding tool/launcher 진입에서도 계약 귀속을 확인한다. 일반 질문 뒤 직접 편집하거나 raw legacy runner를 실행하면 기존 실행 ledger에 명시적 `unverified execution`을 남기고, 원래 메시지·session과 parent 귀속을 해결할 때까지 공식 coding 완료를 거절한다. parent가 없으면 미귀속 실행으로 보존하며 새 parent/계약 없이 검증 완료로 승격할 수 없다. legacy 실행 자체나 일반 대화를 일률적으로 금지하지 않는다.
4. 기존 lifecycle registry를 우선 사용하고, 필요한 parent 작업과 child 실행의 참조·검사 귀속만 최소 확장한다. 기존 registry의 실행 phase를 전체 작업 상태로 그대로 재해석하지 않는다. 새로운 서비스나 병렬 완료 DB는 만들지 않는다.
5. 계약 생성·수정·복원은 사용자 작업 단위로 유지한다. 후속 질문, 상태 요청, 압축, 세션 전환이 새로운 작업으로 오인되거나 기존 요구를 지우지 않게 한다. 일반 질문에는 코딩 검증을 요구하지 않는다.

## 3단계 완료 판정을 계약 집계에 연결

예상: 1 작업일. 다음 단계 착수 조건은 미충족 요구가 있는 동안 Hermes가 소유한 공식 완료 경로가 거절되는 것이다. 외부 consumer의 자체 상태 판정까지 이번 범위에서 보장하지 않는다.

1. process verification은 해당 실행에만 적용한다. 기존 `verified` JSON이 있더라도 parent task 완료 권한으로 사용하지 않는다. parent는 계약의 모든 필수 검사와 유효한 리뷰를 집계한다.
2. trusted aggregator가 DB에서 현재 계약·coverage 영수증·검사 영수증·산출물 revision을 검증해 완료 결정을 생성한다. 결정에는 parent ID·contract digest·검증 revision·필수 검사 결과의 digest를 결합한다. gateway renderer와 Hermes 소유의 외부 작업 완료 통지 지점은 모델의 payload나 acceptance ID를 믿지 않고 이 결정을 권위 있는 저장소에서 조회한다. `PASS / failed / not_run` 및 공식 완료 요약도 실제 영수증에서 생성한다. 모델 본문·수동 완료 요청·child VERIFIED/DELIVERED로 Hermes의 판정을 우회할 수 없다. 운영자 수동 override가 필요하다면 자동 검증 완료와 구분한 감사 기록을 남긴다. 외부 plugin이 별도로 만든 완료 상태의 차단은 이번 보장 범위가 아니다.
3. 필수 조건이 남으면 상태와 남은 항목을 시스템이 출력한다. 모든 일반 응답을 중단시키는 stop-loop로 만들지 않는다. 중간 진행 보고·질문·취소는 허용하며, 실행 예산 소진 시에도 미검증 상태를 보존한다. 검증 follow-up에서 권한상 실행할 수 없는 검사는 `not_run`으로 기록하고 기존 승인 범위의 parent 실행 경로로 돌린다. follow-up 자체의 도구 제한이나 승인 경계를 우회하지 않는다.
4. 압축 후 DB 계약을 다시 참조하고, 재시작 시 기존 영수증으로 복원한다. duplicate completion과 재전송은 멱등 처리한다. 저장 실패나 검증 예외는 완료로 통과시키지 않는다.
5. 동일 산출물에 대한 통과 기록은 재사용하되, 코드·검사·계약·배포본이 달라지면 영향받는 검증을 무효화한다. merge 후 동일성 여부를 검증하며 브랜치 이름이나 서로 다른 commit의 PASS만으로 대체하지 않는다.

## 4단계 Hermes 실제 경로와 외부 전달 경계 검증

예상: 0.5 작업일. 다음 단계 착수 조건은 FIDO 없이 실제 gateway→Mac 실행→검증→Hermes 공식 응답과 외부 전달 경계의 증거가 확보되는 것이다.

1. FIDO와 무관한 작은 저장소에서 실제 gateway·Mac launcher·실제 코딩 worker와 검증 명령을 실행한다. 계약·실행·산출물 revision·검사 결과·공식 완료 결정을 연결해 보관한다. 실제 lifecycle을 fixture로 대체하지 않는다.
2. 필수 검사 미실행·실패·다른 revision의 PASS에서는 작업 완료가 나오지 않고, 모든 필수 검사와 독립 리뷰가 통과한 경우에만 완료되는지 확인한다. CI watcher의 PASS와 실행 종료는 각각의 사실로 남아야 한다.
3. 기존 Hermes hook/event 출구를 격리된 수신기로 관찰한다. 실행 ID·parent ID·원래 session/message·결과의 귀속을 확인하고, 실행 성공/실패/취소와 검증된 parent 완료를 구분한다. 의미 구분이 없는 기존 payload는 자동으로 검증 완료로 승격하지 않는다. 기존 소비자가 읽는 필드의 의미를 조용히 바꾸지 않고 필요한 식별 정보만 호환 가능하게 확장한다.
4. parent 완료 통지에는 3단계의 현재 완료 결정을 참조할 수 있는 정보를 연결한다. 미검증·저장 실패·오래된 결정에서는 통지를 거절하며, 실행 결과는 parent 미완료와 별개로 전달 가능해야 한다. 병렬 실행·재시작·중복 전달도 검사한다. 신규 전송 시스템이나 FIDO 전용 이벤트를 만들지 않는다.
5. FIDO 담당자에게 전달할 Hermes 측 계약과 샘플을 남긴다. 필드와 event의 의미, identity/revision 연결, 중복 처리 기준, 호환 조건, 실제 Hermes 출력 증거와 미검증 범위를 기록한다. 수신기의 계약 검사는 Hermes 출구까지만 증명하며 FIDO E2E를 대신하지 않는다.

## 5단계 별도 리뷰와 단계적 적용

예상: 0.5 작업일. 합계 3.5~4.5 작업일이며 기존 lifecycle의 실제 연결 상태에 따라 달라진다. FIDO 수정·검증·배포 시간은 포함하지 않는다.

1. 구현 리뷰어는 원래 요청, 요청 revision, 전체 diff, required checks, 실제 실행 영수증과 미검증 목록을 받는다. 구현자가 요약한 성공 보고만 넘기지 않는다. read-only 리뷰와 검증 실행 권한을 분리한다.
2. 격리 canary에서 아래 실패·정상 대조군을 통과시킨다. 기존 장치와 새 연결 양쪽의 테스트가 통과해야 한다.
3. 운영 적용 전 source 정본과 Linux/Mac 배포 manifest, 기존 DB의 additive migration/백업·rollback, 진행 중 작업의 처리를 구체화한다. 기존 작업에 추측한 계약을 붙여 완료로 소급 처리하지 않는다. 최소 호환 schema와 enforcement 지원 여부를 capability로 확인하고, 진행 중 run은 launch 당시 release에 고정한다.
4. 이후 실제 배포 범위의 승인·정책에 따라 Mac 계약 실행·검증 지원을 먼저 설치하되 enforcement는 활성화하지 않는다. Linux의 계약 연결·aggregator·renderer·Hermes 외부 전달 경계를 설치하고 양쪽 capability/schema를 확인한 후 canary profile의 enforcement를 활성화한다. FIDO consumer는 설치·재시작하지 않는다. 외부 consumer가 새 완료 결정을 이해하지 못해도 Hermes 자체 게이트는 유지하며 외부 화면 검증은 별도 미완료로 기록한다. 재시작은 진행 중 실행을 확인한 뒤 계획된 창에서 수행한다. canary 성공 후 대상 coding 경로로 확대한다.
5. rollback은 enforcement를 유지하는 호환 이전 release를 사용한다. 그런 release가 없으면 보호된 상태 출력 경로를 유지하고 공식 coding completion을 unavailable/unverified로 닫은 채 일반 대화를 제공한다. 보호 기능 없는 버전을 활성 완료 경로로 되돌리지 않는다. 계약·영수증을 보존하고 이전 worker를 자동 재실행하지 않는다. Linux/Mac 혼합 버전과 downgrade에서도 이 규칙을 검사한다.

## 합격 기준

| 경우 | 기대 결과 |
| --- | --- |
| 하위 unit 검사·CI는 PASS지만 실제 산출물의 필수 검사가 없음 | child 검사만 통과. parent 완료 거절, 누락 항목 표시 |
| 모델이 `verified`를 출력하거나 작업 체크박스를 모두 체크 | 필수 영수증 없으면 parent 완료 거절 |
| 최초 계약부터 원래 요구의 필수 결과를 누락 | contract digest에 묶인 coverage 리뷰가 거절하거나 미확정. 공식 완료 거절 |
| 모델이 원래 요구를 Non-goals로 옮기고 자체 제외 사유·새 revision 생성 | 인증된 사용자 범위 결정이 없으면 변경 거절. 기존 필수 조건 유지 |
| 다른 실행·repo·commit·artifact·배포본·계약의 PASS | 증거 귀속 불일치로 완료 거절 |
| 압축·재시작·timeout·검증 예외·중복 알림 | 미완료 요구 보존, 성공으로 승격 안 됨, 중복 실행 안 함 |
| 일반 질문 뒤 직접 편집하거나 raw legacy runner 실행 | tool/launcher 입구에서 비검증 실행을 남김. parent 미귀속이어도 공식 coding 완료 거절 |
| 진행 중 parent가 있는 후속 질문 | 질문은 정상 응답. 기존 계약과 필수 조건을 변경·종료하지 않음 |
| 모델의 위조 완료 payload·수동 완료 요청·child VERIFIED/DELIVERED | authoritative parent 결정 없이는 Hermes의 parent 완료 통지와 공식 완료 출력 거절 |
| 실제 gateway→Mac 실행 성공·실패·취소·병렬·재시작 | 실제 기록과 Hermes 출구의 실행 identity·결과가 일치. 실행 종료만으로 parent 완료 안 됨 |
| FIDO와 무관한 저장소의 모든 필수 검사와 리뷰가 실제로 통과 | Hermes 공식 완료와 현재 결정에 연결된 완료 통지 가능 |
| 외부 consumer가 과거 payload를 자체적으로 완료로 해석 | Hermes 검증 완료의 증거로 인정하지 않음. consumer 수정·화면 반영은 별도 작업 |
| 일반 질문·진행 상황 문의 | 기존처럼 응답. 불필요한 계약·검사·리뷰 강제 없음 |
| Linux/Mac 혼합 버전 또는 enforcement 없는 release로 downgrade | 호환 확인 전 인증 완료 비활성. 상태를 읽을 수 없으면 unverified. 일반 대화 유지 |

## 현재 완료된 범위

원인 재검토, 현재 소스·실세션 경로 확인, Hermes 전용 적용 계획 작성과 설계 리뷰까지가 문서 작업의 범위다. 기존 리뷰 판정을 이번 개정의 자동 승인으로 간주하지 않으며 정확한 상태는 REVIEW.md에 기록한다. 위 구현과 canary·테스트는 앞으로 수행할 작업이며 통과했다고 주장하지 않는다. 이번 계획은 모델의 의미 판단 오류를 완전히 없애는 장치가 아니라, 누락·범위 변경·증거 불일치가 전체 완료로 승격되는 경로를 줄이는 장치다. FIDO 결함 해결과 전체 제품 E2E는 이 계획의 완료 주장이 아니다.

## 증거 위치

- 실제 Linux runtime: `/home/justin/hermes-v0.22.5-sync-20261026-93ae410994` (SSH `choi138-ri`). systemd는 `v0225-sync-93ae410994` 환경의 gateway를 실행한다.
- 현재 Mac runner: `/Users/choegeun-won/hermes-workspaces/delegation-notifications-20261013/releases/mac-20261027-terra-report`; `hermes-delegate`의 RELEASE 상수 기준이다.
- 사건 당시 runner: 같은 releases 아래 `mac-20261013-170224`. koharu 세션 `20261027_125318_a7c89849`, launch 메시지 `85758`, `86564`에서 확인했다.
- 기존 원칙: `hermes-workspaces/astra-process-review-20261021/repo/skills/orchestration/durable-task-routing/references/verification-contract-gate.md`. 이 문서는 기존 지침의 존재를 보여 주며 현재 세션에서 로드됐다는 증거는 아니다.
- FIDO 제외 범위: `Documents/fido-dashboard/openspec/changes/archive/2026-10-26-fix-auto-substep-reporting/proposal.md:57`.
- FIDO 입력 fixture: `Documents/fido-dashboard/hermes-plugin/tests/test_contract_fidod.py:748`.
- 세션 압축 요약 `88964`에도 운영 Hermes→Discord 전체 흐름이 미검증이라고 남아 있다. 요구를 잊은 것만으로 설명하지 않는다.
- 수집 시각·실행 소스 hash·선택된 소스 원문과 DB 조회 결과는 같은 폴더의 [EVIDENCE.json](EVIDENCE.json)에 보관한다.
