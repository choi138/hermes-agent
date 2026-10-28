# Hermes 완료 판정 해결안 독립 리뷰

작성일: 2026-10-28 Asia/Seoul

리뷰어: 별도 에이전트 `/root/review_completion_solution`. 실행 중인 Linux runtime, 실제 Mac launcher와 koharu 세션 DB를 읽기 전용으로 조사했다. 아래는 리뷰어 결과를 정리한 기록이며, 구현·테스트 통과를 인증하는 문서는 아니다.

## 현재 개정 상태 — Hermes 전용

사용자의 명시적 범위 변경에 따라 PLAN.md에서 FIDO plugin·fidod·API·UI 수정과 FIDO E2E·배포를 제외했다. Hermes gateway·Mac launcher·계약/검증·공식 완료·기존 외부 전달 경계까지만 구현 대상으로 남겼다. 기존 FIDO 작업의 필수 검사를 소급 삭제하거나 통과 처리하지 않는다.

**Hermes 전용 개정본 최종 판정: APPROVE. 남은 P0/P1 설계 차단 사유 없음.** 동일한 독립 리뷰어가 개정 PLAN.md 원문을 다시 읽고 범위 경계와 외부 전달 보장을 검토했다. 기존 통합안에 대한 아래 판정은 역사적 기록이다.

1. FIDO 소유 plugin·fidod·API·UI·명세·테스트·QA·배포가 제외됐고 consumer 설치·재시작도 제외됐다. 과거 사건 증거와 Hermes 측 연동 계약 문서만 남긴 것은 범위 침범이 아니다.
2. contract digest에 결합된 coverage 영수증과 인증된 범위 변경, 실제 tool/launcher 귀속 검사, trusted aggregator 기반 공식 완료·외부 통지라는 이전 P1 보호가 유지됐다.
3. 실제 gateway→Mac worker→실제 검사→공식 응답과 기존 출구 관찰은 Hermes 범위 검증에 충분하다. 미검증·저장 실패·stale 결정은 완료 통지를 거절하고 실행 종료는 별도로 전달한다.
4. 외부 consumer의 자체 상태와 FIDO 화면을 보장하지 않는다는 한계가 명시됐다. 이 제한 때문에 Hermes 게이트를 해제하지 않는다. Mac 선배포·호환성 확인·canary·보호된 rollback도 유지됐다.
5. 이는 설계 승인이다. hook/event 호환성·출구 차단·재시작·병렬·중복·Linux/Mac 통합은 구현 후 증명해야 한다. Hermes 작업 완료를 FIDO 결함 해결이나 제품 전체 E2E 통과로 보고할 수 없다.

## 1차 판정

**수정 후 채택.** 원래 요청과 증거를 연결해 완료를 판정하는 방향은 타당하다. 새 프레임워크를 만드는 방안은 기존 구현을 중복하고 실제 입구의 우회를 남기므로 채택하지 않는다.

| 중요도 | 리뷰 발견 | 계획 반영 |
| --- | --- | --- |
| P1 | 기존 lifecycle이 있으나 실제 Mac `--spec` 경로가 사용하며 `lifecycle` opt-in을 거치지 않음 | 1·2단계에서 실제 입구 연결·legacy 경로의 검증 승격 차단 |
| P1 | CI watcher나 child run 검증이 전체 사용자 요청 완료로 오인될 수 있음 | 3단계 parent acceptance 집계와 공식 완료 상태의 공통 판정 |
| P1 | 원문 보존만으로 최초 acceptance 누락을 막지 못함 | 2단계 원래 요청·관련 이전 요청·Non-goals의 별도 의미 검토 |
| P1 | fixture E2E·브라우저 QA가 실제 Hermes 종료 연결을 증명하지 않음 | 4단계 실제 gateway→Mac→plugin→fidod→UI 검사와 플러그인 수정 |
| P2 | 재시작·권한 제한·오래된 증거·legacy 전환이 누락될 수 있음 | 불변 조건과 3·5단계에 영속 복원·not_run·revision·단계적 전환 추가 |

## 검토의 한계

리뷰는 소스·세션 기록에 대한 설계 검토다. 운영 기능을 변경하거나 새 테스트를 실행하지 않았다. koharu DB의 lifecycle 테이블 부재와 사건 세션의 lifecycle 호출 0건은 이 경로에서 미사용이었다는 근거이며, 다른 profile이나 모든 동적 호출의 부재를 뜻하지 않는다. 의미적 검토는 모델 판단이 남으므로 거짓 완료가 불가능해진다는 주장은 하지 않는다.

## 계획 재검토

1차 발견을 반영한 PLAN.md를 동일 리뷰어에게 다시 전달했다. 판정은 **REVISE, P0 없음, P1 세 가지 보완 필요**였다.

1. 원문 대조 coverage 리뷰를 최초 계약·범위 변경마다 contract digest에 결합한 필수 영수증으로 만들 것. 필수 요구 삭제·완화는 인증된 사용자 범위 결정 없이 모델이 자체 승인할 수 없게 할 것.
2. 코딩 분류기만 믿지 말고 실제 coding tool/launcher 입구에서도 귀속을 확인할 것. parent가 없는 실행도 비검증 상태로 남겨 공식 완료를 거절할 것.
3. gateway renderer와 FIDO parent 완료 event는 모델 제공 payload가 아닌 trusted aggregator의 현재 계약·revision·필수 영수증 결정을 조회할 것. execution 종료와 parent 완료는 구분할 것.

P2 권고로 Mac 지원 선배포→Linux와 consumer 연결→capability/schema 확인→enforcement 활성화 순서와 혼합 버전·rollback의 완료 거절을 구체화했다.

위 항목을 PLAN.md의 불변 조건, 2·3·4·5단계와 합격 기준에 반영했다.

## 최종 재리뷰 판정

**APPROVE — 수정된 적용 계획을 채택할 수 있음. 남은 P0/P1 설계 차단 사유 없음.** 별도 리뷰어가 최신 PLAN.md 원문을 다시 읽고 아래 보완을 확인했다.

1. 최초·변경 계약의 coverage 영수증을 contract digest와 원문 출처에 결합하고 필수로 요구한다. 필수 결과의 삭제·완화에는 인증된 사용자 범위 결정이 필요하다.
2. 실제 coding tool/launcher에서 계약 귀속을 확인하며, parent 없는 실행도 unverified로 보존한다. 일반 대화는 허용하면서 공식 완료 승격을 차단한다.
3. trusted aggregator의 결정을 renderer와 FIDO parent 완료 event의 공통 권위로 사용한다. 실행 종료 표시는 parent 완료와 분리한다.
4. Linux/Mac capability·schema 확인, 진행 중 실행의 release 고정, Mac 지원 선배포, canary 활성화와 보호된 rollback을 계획에 반영했다.
5. 합격 기준은 최초 계약 누락, 임의 revision, 미귀속 실행, 위조 payload, 혼합 버전 downgrade를 포함한다. 기존 lifecycle 재사용과 최소 확장 원칙을 유지한다.

이 판정은 **설계 승인**이다. 실제 우회 차단, lifecycle 내구성, Linux/Mac 통합, FIDO 실제 경로와 rollout/rollback의 동작을 검증한 것은 아니다. 구현 후 실패·정상 대조군과 실제 gateway→Mac→plugin→fidod→브라우저 검사를 통과해야 운영 적용을 판단할 수 있다.

원문 의미를 구현자와 리뷰어가 함께 잘못 해석하거나 테스트가 불충분할 위험은 남는다. 완전한 거짓말 방지를 보장하지 않는다. 이번 작업에서 운영 코드·설정·DB를 변경하지 않았으며, 임시 파일·프로세스를 만들지 않았다.
