# hermes-delegate

koharu가 Mac에 일을 넘길 때 쓰는 진입점이다. 실행 파이썬은 `/usr/bin/python3`(시스템, 표준 라이브러리만)이다.
venv가 사라져도(2026-10-19 `hermes-agent/.venv` 전례) 이 도구는 살아남아 "runner python missing"을 한 줄로 알려준다.
러너 쪽 판정(코드 범위 분류, dry-run)은 러너가 실제로 쓰는 `hermes-agent-upstream/.venv/bin/python`(psutil 포함)으로 돌린다.

이 저장소 트리는 원본 배포 스냅샷을 조상으로 둔 리팩터링이며, 설치된 진입점과 바이트 단위로 같지 않다. 배포되지 않았고 설치된 진입점은 변경하지 않았다. 출처와 모듈별 소유권은 [SOURCE.md](SOURCE.md)에 기록한다.

## SPEC 사전 검사

```sh
hermes-delegate spec-check --template
hermes-delegate spec-check --spec SPEC.md [--workdir DIR] [--kind codex|claude|qa]
```

`--template`은 작성용 골격을 출력한다. 검사 기본 kind는 `codex`, 기본 workdir는 SPEC이 있는 디렉터리다.
통과하면 stdout에 `ok`와 exit 0, 거절하면 stderr에 문제마다 한 줄과 exit 2를 반환한다.
`codex`·`claude`·`qa`의 신규 실행에도 검사를 강제하며 우회 옵션·환경 변수·설정은 없다. `--launch-run`은 기존 동작을 유지한다.

1. `codex`·`claude`는 `## External contracts`, `## Failure paths`, `## Acceptance`가 각각 하나씩 필요하다. 한국어 별칭은 `외부 계약`, `실패 경로`, `완료 검증`이며, `qa`는 Acceptance만 필수다. 제목은 대소문자를 구분하지 않는다.
2. 각 절에는 들여쓰기 없는 `- `, `* `, `1. ` 또는 `1) ` 항목이 필요하다. 외부 계약은 인라인 코드의 실제 파일 경로와 유효한 줄 번호(`path:LINE`, `path:A-B`)로 확인한다. 상대 경로는 workdir, SPEC 디렉터리 순서로 찾으며 절대 경로와 `~`도 허용한다. 다른 참조가 맞아도 잘못된 줄 참조는 거절한다. 단, `localhost:PORT`, IPv4`:PORT`, `[IPv6]:PORT` 모양은 파일 참조로 보지 않는다(도메인`:PORT`는 파일 참조로 읽히므로 코드 표기 밖에 쓴다).
3. 실패 경로에는 Ordering/concurrency, Interruption, Rejection/partial failure, Repeat/retry, External의 다섯 주제를 `라벨: 답변`으로 작성한다. 답변은 공백 제외 15자 이상이어야 한다. `N/A: 이유` 또는 `해당 없음: 이유`는 공백 제외 8자 이상의 이유가 필요하며, 외부 계약에서는 단독 항목으로만 허용한다.
4. 완료 검증에는 N/A를 쓰지 않는다. `1건 이상`, `at least one`, `>=1`, `n≥1`, `etc.`처럼 일부 결과의 존재만 확인하는 표현을 거절한다(앞뒤가 숫자인 `>= 10`, `21건 이상`은 정확한 기준이라 통과). `non-zero`, `nonzero`, `0이 아님`, `> 0`, `!= 0`, `≠ 0`은 바로 앞이 `exit`, `exit code`, `status`, `rc`, `returncode`, `종료 코드`일 때만 통과하고, 개수에 쓰면 거절한다. `non-empty`, `비어 있지 않음`, `적어도 하나`, `최소 한 개`, `1+`, `=> 1`도 거절한다. 검사 전에 공백·줄바꿈을 한 칸으로 합치고 표현 안의 띄어쓰기는 없어도 같은 표현으로 본다(`1건이상`, `at least` 뒤 줄바꿈 후 `one`). 기대한 결과 각각을 정확히 확인하는 조건을 쓴다.
5. SPEC은 비어 있지 않은 UTF-8 파일이며 최대 1 MiB다. 모든 문제를 모아 실행 전에 거절하므로 run/output 디렉터리가 생기지 않는다. Claude/QA는 검사를 통과한 바이트를 `RUN_DIR/spec.md`에 저장한다. Codex 러너는 원래 SPEC 경로를 다시 읽으므로 검사 뒤 파일 변경 가능성은 유지된다.

## 1. Codex 위임: `hermes-delegate codex`

```
hermes-delegate codex --spec SPEC --workdir DIR --class general --timeout 3600 [--risk high] [--code-scope src ...] [--report REL]
```

- 모든 경로를 realpath로 확인한다(심볼릭 링크, `..`, 임시 디렉터리의 심볼릭 링크 별칭 같은 비정규 경로는 거절).
- `--workdir`는 git 작업 트리 루트여야 한다. 하위 디렉터리나 비-git 디렉터리는 실제 실행에서 `registration_baseline_unavailable`(exit 74)로 막히므로 미리 거절한다.
- dry-run 뒤에 러너의 `collect()`를 같은 manifest 조건으로 한 번 더 돌려, 실제 등록 baseline이 통과할 때만 줄을 출력한다.
- `--timeout`은 필수다(러너 기본값 600초는 보통 작업에 너무 짧다).
- `--report REL`은 workdir 안에 쓸 구현 보고서의 상대 경로다. `mechanical`/`bounded`에서는 필수이며 `--sandbox workspace-write`가 필요하다. 경로는 1~256자이고 NUL·줄바꿈·절대 경로·`..` 구성 요소·정규화 결과 `.`은 거절한다. SPEC 본문에도 REL을 그대로 명시해야 한다.
- `--report` 필수 여부는 러너 dry-run이 정한 실효 클래스로도 다시 본다. `--class complex|frontier --phase implementation --contract-resolved --implementation-class bounded`처럼 bounded로 바뀌는 조합도 `--report` 없이는 exit 2로 거절한다. `canary-terra-*` 클래스는 카나리 측정 전용이라 이 요구에서 제외한다.
- `--report`가 있는 실행에서 워커가 0으로 종료해도 workdir 안에 새롭고 비어 있지 않은 일반 보고서 파일을 쓰지 않으면, 러너는 exit 76과 `acceptance.status=failed`로 실패 처리한다.
- `--class`의 별칭으로 `--task-class`도 받는다. `--code-scope`에 디렉터리를 주면 `git ls-files`로 펼치고, 러너의 `file_class`가 받는 파일만 남긴다.
- `--sandbox read-only`는 출력 줄에서 뺀다. 러너 기본값이 read-only이고, 명시하면 `guard_review_gate`가 리뷰로 보고 막기 때문이다. 쓰기가 승인된 작업만 `--sandbox workspace-write`.
- frontier(xhigh)는 `--class frontier --deeper-analysis-evidence <실제 근거>`로 입구에서 바로 된다. max는 `--effort max`가 필요해서 입구 밖이다(사용자 승인 사항).
- 출력 디렉터리가 없으면 `~/.local/state/hermes-delegate/codex/<시각>-<난수>`에 0700으로 만든다(작업 트리 밖이어야 한다).
- `--model`/`--effort`/`--tier`와 그 밖의 override(`--pinned-*`, `--selection`, `--cli`, `--advisor`), `--progress-*`는 거절한다. 모델 선택은 러너 정책만 한다.
- 종료 코드: 0 한 줄 출력, 3 정책상 skipped(워커 불필요, 출력 없음), 74 정책 blocked, 2 사용법 오류, 1 그 밖의 실패.

**왜 실행하지 않고 argv만 출력하나:** 게이트웨이(`tools/delegation_notifications.py` `prepare_launch`)는 terminal 명령이
`python… /…/scripts/run_codex_task.py …` 형태의 맨 argv일 때만 progress manifest/state/thread/label/owner를 주입하고 진행 카드를 붙인다.
래퍼나 셸을 한 겹이라도 씌우면 `completion_only`로 떨어지고 `--progress-login-shell zsh`는 exit 74가 난다.
그래서 koharu는 출력된 줄을 **그대로 단독으로** `terminal(background=true, notify_on_complete=true)`에 넣는다. 이 실행에는 watcher를 따로 걸지 않는다. 띄운 뒤 확인은 출력 디렉터리에 `hermes-delegate status OUTPUT_DIR` 한 번까지다(sleep+peek 금지, 아래 공통 절).

## 2. 리뷰: `hermes-delegate review`

```
review-gate open --item ID --goal "..."        # 아이템은 한 번만 연다(review-gate가 거절하면 그대로 보고)
hermes-delegate review --reviewer claude|codex --item ID --commit SHA --prompt PROMPT.md --workdir DIR --check   # 읽기 전용 사전 점검
hermes-delegate review --reviewer claude|codex --item ID --commit SHA --prompt PROMPT.md --workdir DIR [--base BASE_SHA] [--via direct|cmux] [--owner-approval ID] [--fallback]
```

- 리뷰어 정책은 코드의 `REVIEW_POLICY` 한 곳에 있다.
  - `claude`: `claude -p --model opus --effort high`, 도구는 Read/Glob/Grep만(`--add-dir RUN_DIR`로 run dir 읽기 허용), stream-json. 기본 `--via cmux`(Keychain 로그인). `--via direct`는 cmux 없이 OAuth 토큰 파일로 돈다(아래 "direct 실행").
  - `codex`: `codex exec --ephemeral -s read-only -m gpt-6.1-sol -c model_reasoning_effort="high" --json -o RUN_DIR/review.final.md -`(고정). asdf node 22.19.0의 `codex` 절대 경로와 `ASDF_NODEJS_VERSION=22.19.0`. 기본 `--via direct`(ssh에서 인증됨, 2026-10-22 확인), `--via cmux`도 된다.
- `--workdir`는 `--commit`을 체크아웃한 깨끗한 트리여야 한다(추적 안 된 파일 포함). `inner.zsh`도 실행 직전에 HEAD와 깨끗함을 다시 확인하고, 어긋나면 rc=2와 사유를 남긴다.
- 입구가 run dir에 `diff.patch`(`--base`가 있으면 `git diff --binary BASE COMMIT`, 없으면 `git show COMMIT`)와 `log.txt`(`git log --stat BASE..COMMIT` 또는 `git show --stat COMMIT`)를 미리 만든다. 리뷰어가 git을 직접 돌리지 못해도 diff를 읽는다.
- `prompt.md` = 넘긴 프롬프트 + 입구가 붙이는 표준 규칙: 두 파일 경로, P0/P1만, `file:line`과 구체적 재현 조건이 없으면 제외, 취향·확장 제안 금지, 2차 이상은 이전 지적 해결 여부·수정으로 생긴 회귀·새 diff 결함만, 승인 문장은 정확히 `APPROVED: no P0/P1`.
- `--check`는 티켓을 만들지 않는다. 입력 검증, 실행기(cmux면 `cmux ping`, direct면 CLI 존재·토큰 파일 권한), `review-gate status`로 start가 거절될지까지만 본다. 인증은 `hermes-delegate smoke`로 확인한다.
- 실제 실행은 `review-gate start`로 티켓을 먼저 받는다. 거절 메시지는 그대로 출력하고 nonzero로 끝낸다. 아이템은 자동으로 열지 않는다.
- "Previous tickets must be fully closed"(판정 대기 티켓이 있음) 거절이면 koharu가 그 판정을 직접 재현해 `review-gate close`로 기록하고 다시 시도한다. 재현할 수 없을 때만 사용자에게 보고한다. `--check`의 같은 거절 문구도 이 절차를 안내한다.
- 3라운드 이상은 사용자 승인이 필요하다. koharu가 Discord에서 사용자에게 묻고, 사용자의 승인 메시지 ID를 `--owner-approval ID`(숫자 15~22자리)로 넘긴다. 입구는 형식을 검사하고 `review-gate start --owner-approval ID`로 그대로 넘기며 `meta.json`에 남긴다. `--check`도 같은 ID로 사전 점검하고, 이 아이템에서 이미 쓴 ID는 거절한다(다른 아이템에서 쓴 ID는 `review-gate start`가 거절한다). join 경로는 없다.
- **리뷰어가 실패하면(UNREVIEWED) 새 라운드가 아니라 같은 라운드의 대체로 다시 한다.** 실패한 표를 `review-gate close --verdict UNREVIEWED --reason ...`로 닫은 뒤 `hermes-delegate review --fallback --reviewer <다른 CLI> --commit <실패한 표의 커밋>`을 쓴다. 입구는 `review-gate start --fallback`으로 넘기고 `meta.json`에 `fallback: true`를 남긴다. 대체가 가능한 상태(라운드의 마지막으로 닫힌 표가 UNREVIEWED, BLOCK·대체 없음)에서 `--fallback` 없이 새 라운드를 열려 하면 입구(`--check` 포함)와 `review-gate start`가 둘 다 거절한다. `--owner-approval`이 있으면 통과한다.
- `--no-launch`는 **진짜 티켓을 발급**하고 실행만 건너뛴다. 그 티켓은 열린 채 남으니 `--launch-run RUN_DIR`로 띄우거나 UNREVIEWED로 닫는다. 실행이 실패했을 때도 같은 `--launch-run`으로 다시 띄운다(새 라운드를 쓰지 않는다).
- run dir(`~/.local/state/hermes-delegate/review/…`)의 `inner.zsh`가 `review-gate run --ticket T -- <리뷰어>`를 실행해 `review.out`·`review.err`·`review.rc`를 남긴다.
- `meta.json`에 commit 전체 SHA, tree 해시, reviewer, via, `--base`를 주면 `git diff --binary BASE COMMIT`의 sha256을 남긴다.
- 완료 감시는 출력된 `watcher:` 줄(`hermes-delegate wait RUN_DIR`)을 그대로 한 번 건다(`background=true, notify_on_complete=true, continue_on_complete=true, persist_on_release=true`).
- **판정 기록은 koharu가 한다.** `wait`가 끝나면 koharu가 `review.result.md`의 지적을 직접 재현·확인한 뒤 `review-gate close --ticket T --verdict PASS|BLOCK|UNREVIEWED`를 실행한다(PASS는 `--blockers 0` 또는 생략, BLOCK은 `--blockers N`(1 이상), UNREVIEWED는 `--reason`). 사용자 몫은 3라운드 이상의 승인(Discord 메시지)뿐이고, 그 ID는 koharu가 `--owner-approval`로 넘긴다. 입구는 판정을 자동 기록하지 않는다.

## 3. Claude Code 구현: `hermes-delegate claude`

```
hermes-delegate claude --spec SPEC --workdir DIR --class ambiguous|judgment|mechanical --timeout 3600 [--advisor opus] [--label L] [--no-launch]
```

- 기본은 cmux 워크스페이스(Keychain 로그인). `--via direct`는 cmux 없이 OAuth 토큰 파일로 돈다(아래 "direct 실행"; 사용자 승인 2026-10-22). 기본값은 SOUL·스킬·가드 안내를 고친 뒤에만 direct로 바꾼다. 러너의 `--cli claude`는 읽기 전용만 지원해서 구현 경로가 아니다.
- 모델·effort는 `--class`로만 정한다. 표는 코드의 `CLAUDE_IMPL_POLICY` 한 곳에 있고 `--help`가 그 표를 출력한다(모호한 버그·흐린 명세는 `ambiguous`, 어려운 설계·원인 판단은 `judgment`, Claude로 돌려야 하는 명세형 작업은 `mechanical`). `--model`/`--effort`와 권한·도구·settings override는 거절한다.
- `--advisor opus`는 실행별 선택이다. 그 실행에만 `--settings '{"env":{"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC":""}}'`를 붙이고 settings.json은 건드리지 않는다. 이미 Opus로 도는 클래스(`ambiguous` opus/medium, `judgment` opus/high)에는 거절하므로 지금은 `mechanical`(sonnet)에서만 의미가 있다(정책 v2, 2026-10-27).
- 권한: `--permission-mode acceptEdits --permission-prompts none`(편집은 작업 트리 안에서만 자동 승인, 나머지 프롬프트는 거절), 도구 `Read,Glob,Grep,Edit,Write,Bash`(Bash는 사전 승인), 빈 strict MCP, 세션 저장 없음, stream-json. `gtimeout -k 60 --timeout`으로 감싼다.
- `--workdir`는 깨끗한 git 작업 트리 루트여야 한다. SPEC은 트리 밖이나 ignore된 경로에 둔다. run dir에 `spec.md`로 복사해 stdin으로 넣는다.
- review-gate는 쓰지 않는다(구현이지 리뷰가 아니다). 서버 `guard_review_gate`도 쓰기 가능한 Claude를 리뷰로 보지 않는다.
- run dir: `~/.local/state/hermes-delegate/claude/<label>-<시각>-<난수>/`. `inner.zsh`가 실행 직전에 HEAD·깨끗함을 다시 보고 `claude.out`·`claude.err`·`claude.rc`, 끝난 뒤 `head_after`·`status_after`를 남긴다.

## 4. 기기·시뮬레이터 QA: `hermes-delegate qa`

```
hermes-delegate qa --spec QA-SPEC --workdir DIR --timeout 5400 [--label L] [--no-launch]
```

- 사용자 고정값: Codex `gpt-6-astra`, effort high, sandbox `danger-full-access`(코드의 `QA_POLICY`). 러너가 Astra를 거절해서 러너 밖이다. 모델·effort·sandbox·class 옵션은 하나도 받지 않는다.
- cmux에서 띄우는 이유: private registry 토큰과 코드 서명이 GUI Keychain에 있다(ssh에서는 404, `errSecInternalComponent`). `danger-full-access`인 이유: 시뮬레이터·DerivedData·Metro localhost 소켓은 workspace-write 밖이다.
- cmux 안의 PATH 앞에는 cmux의 codex shim이 있고, 이 shim이 붙이는 hook-trust 경고가 실행을 망가뜨린다. 그래서 asdf node 22.19.0의 `codex`를 절대 경로로 부르고 `ASDF_NODEJS_VERSION=22.19.0`을 설정한다.
- `codex exec --ephemeral ... --json -o RUN_DIR/qa.final.md -`. 출력은 `qa.out`(이벤트), `qa.err`, `qa.rc`.
- `--workdir`는 깨끗한 git 작업 트리 루트여야 한다.

## direct 실행 (`--via direct`)

- 입구가 `inner.zsh`를 `/bin/zsh -ic`로(`.zshrc`의 codex-lb 키가 필요) Python `start_new_session=True`로 분리해 띄우고 바로 `run_dir=`·`rc=`·`via=direct`·`watcher:`를 출력한 뒤 돌아온다. 블로킹하지 않으므로 포그라운드 타임아웃과 무관하다. 런처 출력은 `launcher.log`.
- Claude 토큰: `~/.config/hermes-delegate/claude-oauth-token`(0600, 본인 소유여야 함; 아니면 입구와 `inner.zsh` 둘 다 명확한 오류). 값은 `inner.zsh`가 실행 시점에 읽어 claude를 exec하는 서브셸에서만 `CLAUDE_CODE_OAUTH_TOKEN`으로 export한다. argv·`inner.zsh` 본문·`launch.json`·`meta.json`에는 경로만 들어간다. `ANTHROPIC_API_KEY`·`ANTHROPIC_BASE_URL`은 unset(2026-08-25 `~/.zshenv`가 OAuth를 덮은 전례), stdin은 spec/prompt 파일이나 `/dev/null`.
- Bash 자식 상속: 2026-10-22 실측(direct 구현 run, sonnet)에서 Claude Code Bash 도구의 `printenv CLAUDE_CODE_OAUTH_TOKEN | wc -c`가 0, `env | grep -c '^CLAUDE_CODE_OAUTH_TOKEN='`가 0. Claude Code가 자식 env에서 토큰을 지우므로 구현 run도 direct를 허용한다(`CLAUDE_IMPL_DIRECT`). 이 동작이 바뀌면 이 값을 False로 두면 구현 direct만 거절된다.
- 취소는 `hermes-delegate cancel RUN_DIR [--grace SEC]`로만 한다(cmux·direct 모두). `RUN_DIR/inner.pid`가 아직 이 run의 `inner.zsh`인지 `ps`로 확인하고 SIGTERM을 보낸다. 그러면 `inner.zsh`의 trap이 worker 그룹(또는 review-gate PID)에 TERM을 넘기고 실제 rc를 남긴다. rc를 기본 30초 기다려 `status=finished|no_rc`를 출력하고 rc(없으면 91)로 끝난다. `inner.zsh`가 아직 시작 전이면 락을 대신 잡아 늦게 뜬 런처가 아무것도 시작하지 못하게 하고 rc 143을 남긴다. 취소된 review 티켓은 koharu가 `review-gate close`로 닫는다(대개 UNREVIEWED).
- watcher(`hermes-delegate wait RUN_DIR`)가 받는 SIGTERM/SIGHUP/SIGINT는 run을 멈추지 않는다. 게이트웨이는 의도적 취소뿐 아니라 stop·restart(`kill_all(source="gateway_shutdown")`, persist_on_release 포함)와 턴 타임아웃·ssh 끊김에도 같은 신호를 보내고, 둘을 구별할 정보(빈 `.cancel` 파일)가 없기 때문이다. `wait`는 `status=no_rc`와 `wait stopped by SIG...; the run keeps going` 줄(다시 보는 법·`cancel` 안내 포함)을 출력하고 91로 끝나며 run은 계속된다. 게이트웨이 재시작 뒤에는 진행 중인 run에 `hermes-delegate wait RUN_DIR`를 다시 건다.
- 정리: run을 멈추는 방법은 `hermes-delegate cancel RUN_DIR` 하나뿐이다. exit 91(`status=no_rc`)은 두 가지 뜻이다. (1) `--timeout` 안에 rc가 안 나옴, (2) `wait` 자체가 신호로 끝남. 어느 쪽이든 run은 아직 돌고 있을 수 있으니 `hermes-delegate wait RUN_DIR`를 다시 걸어 계속 보거나, 멈춰야 하면 `cancel`한다. `cancel`의 91은 grace 안에 rc가 안 나왔다는 뜻이다. exit 92(`status=stalled`)도 run은 계속 돌고 있다는 뜻이다(아래 정체 알림).
- qa는 항상 cmux다(GUI Keychain의 registry 토큰·코드 서명).

## 공통: 실행 신뢰성, `--launch-run`, `wait`, `status`, `cancel`, `smoke`

- `inner.zsh`는 첫 줄에서 `mkdir RUN_DIR/lock`으로 원자적 락을 잡는다. 이미 있으면 rc를 쓰지 않고 75로 끝난다(`*.err`에 사유). `--launch-run`도 락·`started_at`·rc가 있으면 거절한다.
- 실행 대상은 `&`로 띄우고 `wait $!`로 기다린다(zsh는 foreground 자식이 끝날 때까지 trap을 미룬다). HUP/TERM/INT를 받으면 worker(claude·qa)는 gtimeout이 만든 프로세스 그룹에 `kill -TERM -- -PID`, review는 `review-gate run` PID에 `kill -TERM PID`를 보내고(리뷰어 그룹 정리·티켓 `ran` 기록은 review-gate의 SIGTERM 핸들러 몫) 그 실제 종료 코드를 rc로 남긴다. 대상이 시작 전이면 129/143/130.
- `claude`·`qa`도 `run_dir=`, `rc=`, `via=`, `watcher:` 줄을 출력한다. watcher 줄을 그대로 한 번 건다. 실행이 실패하면 `hermes-delegate claude|qa --launch-run RUN_DIR`로 다시 띄운다(이미 시작한 run은 거절).
- cmux로 띄우면 `meta.json`에 `cmux_workspace`(예: `workspace:1000080016`)를 저장한다. `wait`가 rc 0을 보면 그 워크스페이스를 닫는다(`--force` 없이 몇 번 재시도). 실패·정체 run은 남긴다.
- `wait`는 `meta.json`의 `kind`(review/claude/qa, 없으면 review)로 파일 이름을 고른다. 출력에 `status=started|finished|never_started|no_rc|stalled` 줄이 있다. 종료 코드는 worker rc 그대로, `started_at`이 grace(기본 180초) 안에 없으면 **90**, 시작은 했는데 rc가 없으면(시간 초과 또는 `wait`가 신호로 끝남) **91**(이전 125/124는 worker rc와 겹쳐서 바꿨다), `<kind>.out`이 M분 동안 자라지 않으면 **92**(`status=stalled`). 요약 전에 run dir 출력에서 `sk-ant-oat` 토큰을 찾으면 그 자리에서 가리고 경고한다.
- 정체 알림: `wait --stall-minutes M`(기본 claude 20, review 20, qa 30, `0`이면 끔). `<kind>.out`의 크기·mtime 변화를 `wait` 자신의 monotonic 시간으로 샘플링해, max(마지막 증가, 이번 `wait` 시작)부터 M분 동안 변화가 없으면 92로 끝난다. 그래서 다시 건 `wait`는 바로 터지지 않고, Mac 잠자기 동안은 세지 않는다. 마지막 증가 시각, 정체 분, 마지막 도구 호출·이벤트 요약(`sk-ant-oat` 토큰은 가림, 같은 호출 반복 횟수 포함), 다시 걸 watcher 두 줄, `the run keeps going; re-run hermes-delegate wait RUN_DIR ... or stop it with hermes-delegate cancel RUN_DIR` 줄을 출력한다. watcher 줄은 실제 run_dir·`HD_BIN` 절대 경로·남은 `--timeout`(최소 300초)·지정했던 `--stall-minutes`와 `background=true, notify_on_complete=true, continue_on_complete=true, persist_on_release=true`가 들어간 `watcher: terminal(...)`(launch 때와 같은 형식)이고, `watcher_no_stall:`은 같은 호출에 `--stall-minutes 0`을 붙인 것이다. 91(시간 초과·신호)일 때도 `status=no_rc` 다음에 `watcher:` 줄을 출력한다. 92마다 `RUN_DIR/stall_count`를 1 올리고 `stall_count=N of 2`를 출력한다. 두 번째 정체부터는 `watcher:` 줄 대신 `stall_limit_reached:` 줄과 `watcher_no_stall:` 줄만 출력하고, 그 뒤 `--stall-minutes` 없이 건 `wait`는 정체를 보지 않는다(`stall_check=off`, 명시한 `--stall-minutes`는 그대로 적용). 다시 걸 때마다 게이트웨이 continuation이 하나씩 쓰이기 때문이다(아래). run은 절대 멈추지 않는다. rc가 나오면 worker rc가, 신호면 91이 우선한다.
- koharu의 92 대응: 요약을 읽고 빌드·테스트·설치처럼 정당하게 조용한 단계면 출력된 `watcher:` 줄을 그대로 한 번 실행한다(더 길게 보려면 그 줄의 `--stall-minutes`만 키운다). 같은 sleep/poll 반복, 입력 대기, 같은 오류 반복처럼 멈춘 것으로 보이면 출력된 `watcher_no_stall:` 줄을 그대로 한 번 실행해 완료 알림을 살려 둔 채, 근거와 함께 사용자에게 보고하고 `cancel` 전에 묻는다. 정체 보고 뒤 run은 취소되지 않은 한 watcher 없이 남지 않는다. 정체를 이유로 수정이나 새 위임을 시작하지 않는다. 91로 다시 걸 때도 출력된 `watcher:` 줄을 그대로 한 번 실행한다. 정체 뒤 다시 거는 것은 매번 사용자 승인 하나에 걸린 continuation 체인 상한(`agent.process_continue_max_chain`, 기본 10, koharu는 기본값)을 하나씩 쓴다. 이 상한은 승인된 위임→검증→커밋→리뷰→PR 전체가 같이 쓰는 예산이다. 그래서 같은 run의 두 번째 92부터는 출력대로 `watcher_no_stall:` 줄만 실행하고 사용자에게 한 번 알린다. watcher를 건 terminal 결과의 `continuation_note`가 `not armed: process_continue_max_chain reached`이면 그 watcher가 끝나도 이어지는 턴이 없으니, 정체 보장에 기대지 말고 바로 사용자에게 run이 더는 자동으로 이어지지 않는다고 알린다.
- `hermes-delegate status PATH`: 기다리지 않고 바로 끝나는 상태 확인(sleep·wait 없음). PATH는 `STATE/review|claude|qa/<name>` run dir 또는 `hermes-delegate codex`가 만든 `STATE/codex/<ts>-<rand>` 출력 dir이다(끝 `/` 허용, 그 밖은 exit 2). **`launched=yes`나 출력된 codex argv를 띄운 뒤에는 `hermes-delegate status PATH`로 많아야 한 번만 확인하고, `sleep`+`pgrep`/`ls`로 들여다보지 않는다.** 완료는 watcher·게이트웨이 알림으로 온다. 출력은 줄마다 `key=value`: `kind=review|claude|qa|codex`, `status=not_started|running|finished|unknown`, 알 수 있으면 `rc=`, `pid=`(살아 있는 inner.zsh 또는 codex runner), `started_at=`, `ended_at=`, `last_output_age_s=`(`<kind>.out`/`events.jsonl` mtime 이후 초), `stall_count=`, codex는 `task_dir=`, `events=`(events.jsonl 줄 수), `runner_status=`(status.json), 빈 출력 dir은 `dir_age_s=`. 마지막 줄은 `hint=`. runner는 status.json을 끝날 때만 쓰므로 codex `running`은 `--output-dir PATH`를 argv에 가진 살아 있는 runner로 판단한다. exit 0(not_started/running/finished), 3(unknown: rc도 살아 있는 프로세스도 없음, rc·status.json 손상, meta kind 불일치), 2(사용법·state root 밖 경로). 토큰 파일은 읽지 않고 출력 값의 `sk-ant-oat`는 가린다.
- review(claude)는 `review.result.md`, 턴·도구 수, verdict 줄을, review(codex)는 `review.final.md`의 마지막 줄을 verdict로 하고 `review.result.md`에 복사, 명령 실행 수를 출력한다. claude는 `claude.result.md`, 턴·도구 수, 모델, 작업 트리 상태를, qa는 `qa.final.md` 마지막 줄, 이벤트 종류별 수, usage, 작업 트리 상태를 출력한다.
- `hermes-delegate smoke codex|claude [--via direct|cmux]`: 인증·실행만 보는 짧은 호출(codex `gpt-6.1-sol` low, claude haiku). `ok …`(exit 0) 또는 `fail …: 사유`(exit 1). 리뷰가 아니고 티켓도 없다. 기본 via는 codex direct, claude cmux. 가드 우회의 주된 사유였던 "auth smoke"를 이것으로 대신한다.

## 기본 via 바꾸기 (Claude cmux → direct)

지금은 바꾸지 않는다(Wave 3, 따로 진행). 바꿀 때 코드 쪽에서 기본값을 말하는 곳은 아래가 전부이고 한 번에 같이 고친다.

- `hermes_delegate_common.py` `REVIEW_POLICY["claude"]["via"]`: claude 리뷰 기본값이자 `smoke claude` 기본값(`cmd_smoke`가 이 값을 쓴다).
- `hermes_delegate_worker.py` `worker_parser`의 `--via` `default="cmux"`와 help `"cmux (default) or direct"`, description `"cmux workspace by default"`.
- `hermes_delegate.py` 모듈 docstring(`Claude: cmux by default`, `claude ... (cmux by default)`), `hermes_delegate_review.py`의 `review_parser` help(`default: claude cmux`), `hermes_delegate_smoke.py`의 `cmd_smoke` help(`default: codex direct, claude cmux`).
- `hermes_delegate.py` `USAGE`: review의 `claude: ... --via cmux (default)`, claude의 `--via cmux (default) runs in a new cmux workspace`, smoke의 `Default via: codex direct, claude cmux`.
- 이 README: 2절 `claude` 기본 `--via cmux`, 3절 "기본은 cmux 워크스페이스", 공통 절 smoke "기본 via는 codex direct, claude cmux".
- 테스트: `test_default_via_per_reviewer`, `SmokeTests.test_claude_default_is_cmux_and_needs_it`, `DefaultViaTests`(코드 안 기본값들이 서로 같은지 본다).

SOUL·스킬·cleanup 문서 쪽 목록은 staging `core/APPLY.md` 4절에 있다.

## 테스트

```
PYTHONDONTWRITEBYTECODE=1 RUFF_NO_CACHE=1 /usr/bin/python3 -m unittest discover -s tests -q
```

위 명령은 이 디렉터리에서 실행한다. 테스트는 `--no-launch`와 stub(`HERMES_DELEGATE_CLAUDE_BIN`, `HERMES_DELEGATE_CODEX_BIN`, `HERMES_DELEGATE_CMUX`, `HERMES_DELEGATE_REVIEW_GATE`, 가짜 토큰 파일 `HERMES_DELEGATE_CLAUDE_TOKEN_FILE`)만 쓰고 실제 Claude·Codex·cmux는 띄우지 않는다. direct 분리 실행 테스트 1개는 stub claude를 `zsh -ic`로 실제 분리 실행한다.
