# 위임 감시 배선 (delegation watch)

위임한 작업이 완료·실패·정체되었을 때 담당자(Vladilena)가 **해당 작업 스레드에**
직접 보고하기 위한 배선. 스크립트는 알림을 보내지 않는다. 보고 주체는 항상 에이전트다.

E2E 검증: 2026-08-21, `#work-orders` 스레드 `1540143103694995466`.
baseline → running(억제) → failed(보고) 전이가 실제 Discord 배달까지 확인됨.

## 구성 요소

| 위치 | 파일 | 역할 |
|---|---|---|
| Mac | `~/bin/delegate-run.sh` | 위임 명령을 감싸 `~/.delegations/<label>/{log,cmd,state.json}` 기록. 알림 없음 |
| Mac | `~/bin/delegation-collect.py` | state를 안정 정렬 한 줄씩 출력 (`<label> <status> exit=<code|->`) |
| 원격(choi138-ri) | `~/.hermes/scripts/delegation-watch--TEMPLATE.py` | monitor_script 템플릿. 라벨을 **파일명**에서 읽음 |
| 원격 | `~/.hermes/scripts/delegation-watch--all.py` | 전체 감시용 사본 |

원격이 스케줄러 호스트다 (`hermes-main-runtime-reintegration-20260803`).
`cron/monitor.py`가 있어야 `monitor_script`가 동작한다.

## 설치

이 디렉터리가 원본이다. 두 호스트에 배치해야 동작한다.

```bash
# 1. 위임이 실제로 실행되는 호스트 (Mac)
mkdir -p ~/bin
install -m 755 scripts/delegation/delegate-run.sh      ~/bin/
install -m 755 scripts/delegation/delegation-collect.py ~/bin/

# 2. 스케줄러 호스트 (cron이 도는 곳)
scp scripts/delegation/delegation-watch--TEMPLATE.py \
    choi138-ri:/home/justin/.hermes/scripts/
```

`monitor_script`는 `~/.hermes/scripts/` 안의 파일만 허용하므로 템플릿은 반드시
그 경로에 있어야 한다 (심볼릭 링크 대신 복사를 권장 — 컨테인먼트 검증이
`resolve()` 후 경로를 확인한다).


## 상태 값

`running` `done` `failed` `stalled` `unreachable` `broken` `none`

- `stalled` = running인데 로그 mtime이 20분 이상 정지 (`DELEGATION_STALL_SECONDS`)
- `unreachable` = Mac SSH 실패. **작업 실패가 아니다.** 상태 불명
- `none` = 추적 대상 없음

## 사용법

### 1. 위임 실행

```bash
~/bin/delegate-run.sh <label> -- <명령...>
```

라벨은 `[A-Za-z0-9._-]+`만. 경로와 감시 스크립트 파일명에 들어간다.

장시간 codex는 기존 방식과 조합한다 (MEMORY의 `zsh -lic` 규칙 유지):

```bash
screen -dmS <label> ~/bin/delegate-run.sh <label> -- \
  zsh -lic 'codex exec -C /abs/path --skip-git-repo-check < /tmp/prompt.txt'
```

### 2. 감시 잡 생성 (위임과 같은 턴에)

템플릿을 라벨 이름으로 복사한다. 본문은 수정하지 않는다.

```bash
ssh choi138-ri 'cd ~/.hermes/scripts && \
  cp delegation-watch--TEMPLATE.py delegation-watch--<label>.py && \
  chmod +x delegation-watch--<label>.py'
```

그 다음 `cronjob(create)`:

- `monitor_script`: `delegation-watch--<label>.py`
- `deliver`: `discord:<channel_id>:<thread_id>` ← 위임을 지시받은 그 스레드
- `schedule`: `every 3m` (검증 때는 `every 1m`)
- `prompt`: 아래 템플릿

작업이 끝나면 잡을 `remove`하고 사본 스크립트도 지운다.

## 프롬프트 템플릿

`<label>`을 치환해서 쓴다.

```
너는 Vladilena(레나)이고, 위임 작업 "<label>"의 담당자다.

프롬프트 앞에 "MONITOR CHANGE DETECTED" 또는 "Monitor Baseline" 컨텍스트 블록이
붙어 있다. 상태 줄은 `<label> <status> exit=<code|->` 형식이고, status는:
running(실행 중) / done(정상 종료) / failed(비정상 종료, exit_code 참조) /
stalled(로그 20분 정지) / unreachable(Mac SSH 실패, 상태 불명 — 작업 실패 아님) /
broken(state.json 손상) / none(추적 대상 없음)

1. status가 running 또는 none이면 알릴 것이 없다. 정확히 `[SILENT]` 한 줄만
   출력하고 끝낸다. 다른 말을 덧붙이지 않는다.
2. status가 done/failed/stalled/broken이면 로그를 직접 읽고 원인·결과를 확인한다:
   `tail -60 ~/.delegations/<label>/log`
   그 뒤 한국어 해요체로 간결히 보고한다. 스크립트 출력을 그대로 붙이지 말고,
   담당자로서 확인한 사실을 말한다: 무엇이 어떻게 끝났는지, 로그에서 확인한 근거,
   필요한 다음 조치.
3. status가 unreachable이면 작업 실패로 단정하지 말고, Mac 접속이 안 되어 상태를
   확인할 수 없다고만 짧게 보고한다.

추측을 사실처럼 말하지 말고, 확인한 것만 보고한다.
```

## 함정 (검증 중 실제로 밟은 것들)

1. **`monitor_script`는 인자를 못 받는다.** `_validate_cron_script_path`가 경로
   전체를 하나의 파일명으로 검증한다. 그래서 라벨을 파일명에 넣는다.

2. **프롬프트에서 로그를 읽을 때 `ssh geunwon-mac`을 쓰지 않는다.**
   cron 에이전트의 terminal 백엔드가 **이미 Mac**이다. 원격에서 SSH를 시도하면
   `Could not resolve hostname geunwon-mac`으로 실패한다. `tail`을 바로 쓴다.
   (`geunwon-mac` alias는 원격 *셸*에만 있고 에이전트 터미널 컨텍스트에는 없다.)
   감시 **스크립트**는 반대로 원격에서 실행되므로 SSH가 필요하다 — 혼동 금지.

3. **`[SILENT]`을 정확히 쓴다.** "no report needed" 같은 자연어는 그대로 스레드에
   배달된다. `[SILENT]`에 다른 내용을 섞으면 억제되지 않는다.

4. **감시 스크립트는 SSH 실패에 nonzero를 반환하면 안 된다.** `cron/monitor.py`는
   source 실패를 매 틱 경고하고 해시를 갱신하지 않는다 → SSH가 흔들리면 3분마다
   경고 폭탄. 그래서 `unreachable` 상태 줄 + exit 0으로 처리한다.

5. **collector 출력에 흔들리는 값을 넣지 않는다.** 타임스탬프·경과시간·바이트 수를
   넣으면 매 틱이 변경으로 보여 침묵이 깨진다. 상태와 exit code만.

6. **cron 잡은 새 cron 잡을 못 만든다.** 감시 잡은 위임하는 시점에 에이전트가
   같이 만들어야 한다. 코드로 강제되지 않는 운용 규칙이다.

7. **cron 스크립트는 env가 스크럽된다** (SECURITY.md §2.3). 감시 스크립트 인증은
   디스크의 SSH 키에만 의존해야 한다 (`BatchMode=yes`, passphrase 없음).

## 한계

- 최대 지연 = 스케줄 간격(권장 3분). 즉시 알림이 아니다.
- 대신 게이트웨이 재시작에도 생존하고, 보고 주체가 스크립트가 아니라 담당자다.
