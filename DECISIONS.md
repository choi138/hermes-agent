# Hermes fork decision log

## ADR-001 — 포크 패치 관리 정책

- **상태:** 채택(정책·도구), 활성화 대기(브랜치 재편·배포)
- **결정일:** 2026-08-14
- **범위:** `choi138/hermes-agent`가 `NousResearch/hermes-agent` 위에 유지하는 로컬 변경

### 맥락

현재 포크의 장기 실행 변경은 `hermes/all-work` 한 브랜치에 계속 누적된다. 조사
시점의 `hermes/all-work`는 마지막 공통 upstream 커밋
`f15a38ee73631b3cd5f7d30765c37d5f0245d403` 이후 로컬 148개 커밋을 포함하고,
현재 `upstream/main`에는 그 공통점 이후 1,195개 커밋이 있다. 서로 무관한 변경을
한 브랜치에서 함께 운반하므로 한 기능만 제외하거나, 어느 변경이 upstream에 이미
들어갔는지 판단하거나, 충돌을 기능 단위로 해결하기 어렵다.

M3에서는 약 5,000줄 규모의 모델 라우팅 서브시스템이 추가될 예정이다. 포크 전용
신규 파일 4개와 `gateway/run.py`의 큰 변경을 기존 단일 브랜치에 더하면 다음
upstream 동기화에서 기존 변경과 라우팅 변경이 한꺼번에 충돌하고, 라우팅만 골라
되돌리는 것도 불가능해진다.

운영 환경도 단순한 단일 체크아웃이 아니다.

- 이 저장소에는 2026-08-14 현재 29개 worktree가 등록되어 있다. 그중
  `hermes/all-work`는
  `/Users/choegeun-won/Documents/hermes-agent/.worktrees/latency-quality`에 체크아웃되어
  있고, M1과 M2도 각각 별도 worktree에서 진행 중이다.
- 로컬 개발 venv와 설치용 venv는 PEP 660 editable install을 사용한다. 생성된
  `__editable__.hermes_agent-*.pth`/finder는 각각 특정 체크아웃의 절대 경로를
  가리킨다.
- 실제 게이트웨이는 Mac 체크아웃이 아니라 원격 Linux 호스트 `choi138-ri`의
  `/home/justin/hermes-main-runtime-reintegration-20260803`에서 실행된다. systemd의
  `hermes-gateway.service`도 그 체크아웃 안의 `.venv/bin/python`을 실행하며, 원격
  editable finder 역시 같은 원격 체크아웃을 가리킨다.
- 정책상 `main`은 upstream 미러가 되어야 하지만 현재 로컬 `main`과
  `origin/main`은 `upstream/main` 대비 각각 1개 고유 커밋이 있고 7,178개 커밋이
  뒤처져 있다. 따라서 `main`을 미러로 만드는 일도 보존·검토가 필요한 별도
  마이그레이션이며, 이 ADR 도입 과정에서 reset하지 않는다.

이번 M2에서는 정책 문서와 도구만 만든다. `hermes/all-work` 해체,
`hermes/production` 생성, topic 이동, 원격 배포는 사람의 검토와 별도 승인 전에는
실행하지 않는다.

### 결정

포크 변경을 **pinned base + 독립 topic branches + manifest + derived production**
모델로 관리한다.

1. `PATCHES.md`가 pinned upstream base와 포크 패치의 수명 주기·배포 포함 여부를
   기록하는 단일 인벤토리다.
2. 각 변경은 `hermes/patches/<short-name>` 한 브랜치에 한 관심사만 담고, 가능한
   한 모두 같은 `base_commit`에서 직접 출발한다. 의존 패치는 rationale에 선행
   관계를 명시하고 최소화한다.
3. `hermes/production`은 pinned base와 manifest에서 `enabled: true`인 active
   topic들을 octopus merge한 **파생 브랜치**다. 이 브랜치에서 직접 작업하거나
   충돌을 해결하지 않는다.
4. 정책 문서와 관리 도구는 `hermes/fork-policy`에 둔다. 런타임 production stack에
   정책 파일 자체를 패치로 섞지 않는다.
5. 패치의 upstream 수명 주기(`state`)와 production 포함 여부(`enabled`)를
   분리한다. `state`는 아래 네 값만 허용하고, 긴급 제외는 상태를 거짓으로 바꾸지
   않고 `enabled: false`로 표현한다.
6. 모든 변경 명령은 기본 dry-run이다. `--apply` 없이는 fetch, ref 이동, rebase,
   commit 생성, manifest 갱신을 하지 않는다. 도구는 push나 force push를 수행하지
   않는다.
7. topic rebase와 production ref 갱신 전에 `git worktree list --porcelain`을 읽어
   조작 대상 브랜치가 어느 worktree에든 붙어 있으면 거부한다.

### 대안 비교

| 대안 | 장점 | 기각 이유 |
|---|---|---|
| `hermes/all-work` 단일 브랜치 유지 | 지금 당장 구조 변경이 없다 | 서로 무관한 148개 로컬 커밋과 향후 M3가 한 충돌 단위가 된다. 기능별 rollback·upstream 병합 감지·provenance 검증이 불가능하다. 현재 문제를 만든 운영 방식이므로 유지하지 않는다. |
| quilt/patch-series | 적용 순서가 명시적이고 오래된 vendoring 도구가 풍부하다 | 텍스트 패치를 별도 진실로 만들며 Git commit, blame, trailer, worktree, `git patch-id` 흐름을 약화시킨다. 이 포크는 이미 Git branch와 commit 중심으로 개발·검증하므로 이중 관리 비용이 더 크다. |
| upstream 전체를 강제 vendoring하고 fork history로 대체 | upstream 변화로부터 즉시 격리된다 | upstream provenance와 부분 동기화 가능성을 잃고, 보안·버그 수정 수용이 수동 포팅으로 바뀐다. `main` 강제 이동이나 force push도 필요해 현재 안전 제약과 맞지 않는다. `vendored`는 정말 의도적으로 upstream과 갈라지는 개별 topic의 상태로만 허용한다. |

### 브랜치 규약

| 브랜치 | 역할 | 규칙 |
|---|---|---|
| `main` | `upstream/main`의 로컬 미러 | 최종 정책상 로컬 고유 commit을 두지 않는다. 현재 divergence 해소는 고유 commit 보존 방안을 승인한 뒤 별도 수행한다. |
| `hermes/fork-policy` | `DECISIONS.md`, `PATCHES.md`, 관리 도구 | 런타임 stack에 merge하지 않는다. base 변경과 manifest 변경은 검토 가능한 commit으로 남긴다. |
| `hermes/patches/<name>` | 단일 목적 포크 패치 | pinned base에 rebase한다. unrelated change나 production merge commit을 넣지 않는다. |
| `hermes/production` | 배포 후보 파생 ref | 도구로만 재구성한다. 직접 commit, conflict fix, rebase, reset 금지. |
| `agent/*`, `work/*`, `direct/*` | 실험·준비 worktree | production 입력이 아니다. 검토 완료 후 필요한 commit만 정식 topic으로 옮긴다. |
| `hermes/all-work` | 레거시 통합 브랜치 | 마이그레이션 완료 전 읽기·비교 기준으로 보존한다. 이 ADR만으로 이동·삭제·reset하지 않는다. |

`<name>`은 소문자 영숫자와 하이픈만 사용한다. 한 topic이 다른 topic을 전제로 할
경우 manifest에 그 이유와 순서를 명시한다. 순환 의존은 허용하지 않는다.

### manifest 규약

각 entry의 필수 필드는 `branch`, `origin`, `upstream_pr`, `state`, `rationale`,
`commits`, `touches`다.

- `state`: `local-only | pending-upstream | merged-upstream | vendored`
- `enabled`: production 포함 여부. 생략 시 `true`; `merged-upstream`은 항상 inactive.
- `example`: M2처럼 아직 topic으로 분리되지 않은 설명용 entry에만 사용한다.
  `true`인 entry는 production 입력이 아니며 실제 전환 뒤 제거해야 한다.
- `commits`: base 이후 topic commit을 오래된 순서로 기록한다. 실제 active topic에서
  `TBD`는 허용하지 않는다.
- `touches`: 충돌 예측과 표적 테스트 선택에 쓰는 예상/실제 파일 목록이다.

`state`는 upstream 관계를 뜻한다. 장애 때문에 잠시 배포에서 빼는 것은
`merged-upstream`으로 위장하지 않고 `enabled: false`로 기록한다. upstream에 같은
변경이 들어온 것은 `git patch-id` 결과와 사람의 동작 검토를 함께 거친 후에만
`merged-upstream`으로 전환한다.

### commit trailer 규약

`hermes/patches/*`의 모든 commit은 본문 끝에 다음 trailer를 가진다.

```text
Origin: local:<author> | cherry-pick:<sha> | upstream-pr:<number>
Upstream-PR: <number-or-url> | none
Patch-State: local-only | pending-upstream | vendored
```

- `Origin:`은 코드가 최초로 생긴 곳을 기록한다. cherry-pick하거나 재작성했더라도
  원저작 provenance를 지우지 않는다.
- `Upstream-PR:`은 없으면 문자 그대로 `none`을 쓴다.
- `Patch-State:`는 commit 작성 당시 상태다. 나중에 upstream merge가 확인되어도
  과거 commit을 고쳐 쓰지 않고 manifest만 `merged-upstream`으로 바꾼다.
- `bin/hermes-patches add --apply`는 checkout이나 hook 설치 없이 trailer가 포함된
  빈 scaffold commit을 생성한다. 이후 실질 commit도 같은 trailer를 유지해야 하며
  `validate`가 검사한다.

### upstream sync 절차

1. 정책 worktree와 대상 topic들이 clean한지 확인하고, `git worktree list
   --porcelain`로 topic이 다른 worktree에 붙어 있지 않은지 확인한다. prunable로
   표시된 stale record도 자동 무시하지 않는다.
2. ref·branch·worktree 기준 상태를 저장하고 먼저 읽기 전용 계획을 본다.

   ```bash
   python3 bin/hermes-patches status
   python3 bin/hermes-patches validate
   python3 bin/hermes-patches sync upstream/main
   ```

3. dry-run이 제시한 새 base와 각 topic의 patch-id 결과를 사람이 검토한다.
   `vendored`는 patch-id가 일치해도 자동 제외하지 않는다. patch-id 일치는 코드
   내용의 후보 신호일 뿐 동작·설정 계약까지 같다는 증명은 아니다.
4. 승인 후에만 `sync upstream/main --apply`를 실행한다. apply는 `upstream`을
   fetch하고, 임시 staging refs/worktrees에서 각 topic을 새 base로 rebase한다.
   모든 topic이 성공한 뒤 실제 topic refs를 갱신하고 `PATCHES.md`의 base·commit
   목록·상태를 갱신한다. 충돌 시 실제 topic과 production은 움직이지 않는다.
5. 변경된 manifest와 backup ref 이름을 검토하고 정책 commit으로 남긴다. 각
   topic의 `touches`에 대응하는 표적 테스트를 실행한다.
6. `rebuild` dry-run을 검토한 뒤 `rebuild --apply`로 production을 strict octopus
   merge한다. octopus가 충돌하면 production은 그대로 두고 topic에서 해결한 뒤
   다시 시작한다.
7. production 전체 회귀 테스트와 gateway 표적 테스트가 통과한 뒤에만 별도 배포
   절차로 원격 checkout을 갱신한다. 이 도구는 remote push, 원격 SSH 배포,
   gateway restart를 하지 않는다.

### rollback 절차

#### 패치 하나 제외

1. 해당 entry의 upstream `state`는 유지하고 `enabled: false`로 바꾼다.
2. `validate`와 `rebuild` dry-run을 검토한다.
3. 승인 후 `rebuild --apply`로 새 production commit을 만든다.
4. 표적 테스트 후 그 exact production SHA를 배포한다.

topic branch와 commit은 삭제하지 않으므로 `enabled: true`로 되돌려 재적용할 수
있다.

#### 전체 포크 패치 제외

모든 active entry를 `enabled: false`로 한 정책 commit을 만들고 production을
재구성한다. 이때 기준은 움직일 수 있는 `main`이 아니라 manifest의 exact
`base_commit`이다. 긴급 상황에서도 `hermes/production`을 임의 reset하지 않는다.

#### sync/rebuild 자체 되돌리기

apply 명령은 실제 refs를 움직이기 전에 `refs/hermes/backups/...` 아래에 이전 OID를
보존한다. worktree 연결 상태와 현재 OID를 다시 확인한 뒤 exact backup OID로
`git update-ref <ref> <old> <current>`를 실행하면 비교 후 원자적으로 되돌릴 수
있다. 이 수동 동작 역시 사람 승인 대상이며, 도구가 remote ref를 바꾸지는 않는다.

원격 `choi138-ri`는 별도 저장소이므로 로컬 rollback만으로 실행 중 코드가 바뀌지
않는다. 원격은 승인된 exact SHA를 별도 배포하고 gateway 재시작·로그 확인을 해야
한다.

### 안티골

- `hermes/production`에서 직접 수정하거나 충돌을 해결하지 않는다.
- `hermes/all-work`, `main`, 기존 topic을 이 정책 도입만을 이유로 reset·삭제하지
  않는다.
- 관리 도구는 push, force push, remote branch 삭제를 하지 않는다.
- manifest에 없는 branch를 production에 섞지 않는다.
- 한 topic에 관계없는 기능, 포맷 정리, vendored dependency를 함께 넣지 않는다.
- trailer를 나중에 맞추기 위해 공개된 history를 다시 쓰지 않는다.
- patch-id 일치만 보고 동작 검토 없이 패치를 제거하지 않는다.
- Mac의 local worktree가 곧 원격 live gateway라고 가정하지 않는다.
- 한 worktree에서 다른 checkout을 가리키는 공유 venv를 무심코 재설치하지 않는다.
- M3 모델 라우팅 도입과 레거시 branch 재편을 한 번의 대형 변경으로 묶지 않는다.

### 함정

1. **29개 worktree와 shared refs:** 한 worktree의 branch 생성·rebase·삭제는 같은
   저장소의 모든 worktree가 즉시 본다. 특히 `hermes/all-work`는 이미 다른
   worktree에 붙어 있다. Git이 checkout된 branch 이동을 거부하는 것은 안전장치지
   오류가 아니며, 우회해서 ref를 강제로 움직이면 열린 작업 디렉터리와 index가
   어긋난다. stale/prunable record도 소유자 확인 없이 prune하지 않는다.
2. **editable install은 경로를 고정한다:** 로컬
   `/Users/choegeun-won/Documents/hermes-agent/.venv`는 주 checkout을,
   `/Users/choegeun-won/.hermes/hermes-agent/venv`는 설치 checkout을 가리킨다.
   같은 checkout 안에서 Python source만 바꾸는 branch 전환은 `.pth` 재생성이
   필요 없다. 반면 `pyproject.toml`, entry point, dependency/extra, checkout 경로가
   바뀌면 reinstall이 필요하다. 다른 worktree에서 공유 venv를 reinstall하면
   finder가 그 worktree로 재지정될 수 있으므로 금지한다.
3. **원격 live checkout은 별도다:** `choi138-ri`의 실행 명령은
   `/home/justin/hermes-main-runtime-reintegration-20260803/.venv/bin/python -m
   hermes_cli.main gateway run`이고 editable mapping도 같은 경로다. 원격 저장소에는
   조사 시 `origin`만 있고 `upstream` remote가 없다. 로컬 `sync`/`rebuild`가 원격
   코드나 실행 프로세스를 갱신한다고 생각하면 안 된다.
4. **`main`은 아직 미러가 아니다:** 현재 1개 고유 commit을 보존하지 않고
   `upstream/main`으로 맞추면 데이터 손실이다. 먼저 그 commit의 의미와 어느
   topic으로 옮길지 결정해야 한다.
5. **pinned base와 움직이는 ref는 다르다:** `base_ref: upstream/main`은 관찰 대상,
   `base_commit`은 재현 기준이다. status에서 ref가 앞섰다는 이유만으로 manifest를
   자동 갱신하지 않는다.
6. **M1 entry는 아직 topic이 아니다:** `agent/m1-anthropic-proxy-compat`는
   `hermes/all-work`에서 분기한 진행 worktree다. M2 manifest의 entry는 형식 예시일
   뿐 production 입력이 아니며, 완료 SHA가 나온 뒤 정식 topic으로 옮길 방법을
   사람이 정해야 한다.
7. **octopus conflict는 topic 설계 신호다:** sequential fallback으로 production에서
   임시 해결하면 어떤 topic이 어떤 해결을 소유하는지 사라진다. 공통 수정은 별도
   선행 topic 또는 한 topic의 책임으로 정리한다.
8. **코드 rollback과 config rollback은 별개다:** provider key나 model route처럼
   포크 코드가 추가한 config를 사용하는 배포에서 해당 patch만 빼면 시작 실패가
   날 수 있다. `touches`뿐 아니라 원격 `config.yaml` 호환성도 배포 전에 검사한다.

### 검증 방법

정책/manifest 자체는 다음 읽기 전용 명령으로 검증한다.

```bash
python3 bin/hermes-patches status
python3 bin/hermes-patches validate
python3 bin/hermes-patches rebuild
python3 bin/hermes-patches sync upstream/main
```

dry-run 안전성은 실행 전후 다음 세 출력을 byte-for-byte 비교한다.

```bash
git rev-parse hermes/all-work
git branch -a
git worktree list
```

실제 migration 승인 뒤에는 추가로 다음을 검증한다.

- 모든 enabled topic에 대해 `base_commit`이 ancestor이고 manifest commit 목록과
  `base_commit..branch`가 일치한다.
- 모든 topic commit에 필수 trailer가 있다.
- `hermes/production`의 첫 parent는 pinned base이고 나머지 parents는 manifest의
  enabled topic tip과 일치한다.
- 각 entry의 `touches`에 대응하는 표적 테스트와 전체 production 회귀 테스트가
  통과한다.
- 원격 배포 후 systemd가 의도한 checkout/venv를 사용하고, gateway startup 및
  실제 메시지 smoke test가 통과한다.

### 결과

이 정책은 upstream 동기화 충돌을 patch 단위로 격리하고, M3 같은 대형 포크 기능을
기존 변경과 독립적으로 포함·제외할 수 있게 한다. 대가로 manifest와 trailer를
정확히 유지해야 하며, production은 언제나 재생성 가능한 파생물로 취급해야 한다.
현재 branch 구조와 live deployment는 이 문서만으로 바뀌지 않는다.

## ADR-002 — 포크 기준점 재고정

- **상태:** 채택(Accepted)
- **결정일:** 2026-08-20
- **범위:** 레거시 포크 이력의 통합과 `PATCHES.md` pinned base 재정의

### 맥락

기존 manifest는 `hermes/all-work@6715ceafa6c87f300900a6d58d39ae011c9c3adb`를
pinned base로 삼고 그 위에 포크 변경을 독립 topic으로 재생할 수 있다고 전제했다.
그 사이 upstream은 약 2,996개 커밋 앞서 갔고, 기존 topic들은 실제로 서로 독립적이지
않았다. 예를 들어 `hermes/patches/refusal-chain`은 runtime-control ancestry에
의존하면서 merge commit 16개를 포함한 117개 커밋을 운반했다. 따라서 기존 topic을
각각 pinned base 위에 replay하는 방식으로는 같은 포크 상태를 재구성할 수 없었다.

### 결정

`upstream/main@4511ba49dd5830062ffbcfbdb3f2a4fc7f278ccb` 위에 레거시 포크
이력 전체를 하나의 squash integration commit으로 통합하고, 그 통합과 후속 결함
수정을 담은 `hermes/patches/legacy-integration`을 새 기준 브랜치로 사용한다.
manifest의 pinned base를
`hermes/all-work@6715ceafa6c87f300900a6d58d39ae011c9c3adb`에서
`hermes/patches/legacy-integration@fd6e8fb67bb0588708cb2979a53437cdfd2e3b5c`로
재고정한다. 앞으로는 이 기준점에 포함되지 않으면서 독립적으로 replay 가능한 신규
작업만 별도 topic으로 관리한다.

### 결과

동기화 이전 레거시 이력은 하나의 통합 단위가 되므로 그 안에 있던 변경을 과거 topic
단위로 rollback하는 능력은 잃는다. 대신 최신 upstream 위에서 재현·replay 가능한
명확한 기준점을 얻고, 이미 흡수된 topic의 상호 의존성과 merge ancestry를 매번 다시
해석할 필요가 없어진다. 이후 upstream sync에서는 legacy integration 기준점과 소수의
outstanding 독립 topic만 검토·replay하면 된다.
