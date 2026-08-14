# Hermes fork patch manifest

`NousResearch/hermes-agent` 위에 유지하는 모든 포크 런타임 패치의 인벤토리다.
관리 정책은 `DECISIONS.md`의 ADR-001을 따른다.

이 파일과 관리 스크립트는 `hermes/fork-policy`에서 관리하고,
`hermes/production`에는 직접 포함하지 않는다. `hermes/production`은 pinned base와
아래의 활성 topic branch만으로 재구성한다.

> **M2 상태:** 아직 실제 branch 재편을 수행하지 않았다. 아래 M1 항목은 entry
> 형식과 예정 범위를 보여 주는 전환 전 예시이며 production 입력이 아니다.

## Pinned Base

- **upstream:** `NousResearch/hermes-agent`
- **base_ref:** `upstream/main`
- **base_commit:** `f15a38ee73631b3cd5f7d30765c37d5f0245d403`
- **pinned_at:** `2026-08-14T11:42:56+09:00`

`base_commit`은 2026-08-14에 다음 읽기 전용 명령으로 확인했다.

```bash
git merge-base hermes/all-work upstream/main
```

`base_ref`는 움직이는 관찰 대상이고 `base_commit`은 재현 가능한 stack 기준이다.
ref가 전진해도 `sync --apply`와 manifest 검토 없이 pinned commit을 바꾸지 않는다.

## Patch entry format

각 실제 패치는 번호가 있는 `###` section 하나로 기록한다.

```markdown
### N. <short-name>

- **branch:** `hermes/patches/<short-name>`
- **origin:** `local:<author>` | `cherry-pick:<sha>` | `upstream-pr:<number>`
- **upstream_pr:** `none` | `<number-or-url>`
- **state:** `local-only` | `pending-upstream` | `merged-upstream` | `vendored`
- **enabled:** `true` | `false`
- **example:** `false`
- **rationale:** 포크에서 이 패치를 유지하는 구체적 이유와 제거 조건
- **commits:**
  - `<full-or-abbreviated-sha>` commit subject
- **touches:**
  - `path/to/file.py`
```

필수 필드는 `branch`, `origin`, `upstream_pr`, `state`, `rationale`, `commits`,
`touches`다.

- `state` 허용값은 정확히 `local-only | pending-upstream | merged-upstream |
  vendored`다.
- `enabled`는 배포 포함 여부이며 생략하면 `true`다. `merged-upstream`은 값과
  관계없이 비활성이다.
- `example`은 migration 전 문서 예시에만 사용한다. 생략하면 `false`다. 실제
  production stack에 예시 entry를 남기지 않는다.
- `commits`는 `base_commit..branch`의 오래된 순서다. 실제 active entry에는 `TBD`를
  허용하지 않는다.
- `touches`는 충돌 예상과 표적 테스트 선택에 충분할 만큼 구체적으로 적는다.
- 패치를 잠시 rollback할 때 `state`를 왜곡하지 말고 `enabled: false`를 사용한다.

## Patches

현재 활성 patch entry는 없다.

### 1. anthropic-proxy-compat

- **branch:** `agent/m1-anthropic-proxy-compat`
- **origin:** `local:choi138` (design reference: `Soju06/hermes-agent`)
- **upstream_pr:** `none`
- **state:** `local-only`
- **enabled:** `false`
- **example:** `true`
- **rationale:** M1에서 private Anthropic-compatible proxy를 안전하게 지원한다. opt-in signature passthrough, OpenAI `response_format`의 Anthropic `output_config.format` 변환, fallback chain의 명시적 `api_mode` 우선순위를 각각 이식한다. 현재 branch는 레거시 `hermes/all-work`에서 분기한 작업 branch이므로 M1 완료와 사람 검토 전에는 정식 topic이나 production 입력으로 간주하지 않는다.
- **commits:**
  - `TBD`
- **touches:**
  - `agent/anthropic_adapter.py`
  - `agent/conversation_loop.py`
  - `hermes_cli/config.py`
  - `agent/auxiliary_client.py`
  - `agent/chat_completion_helpers.py`
  - `tests/agent/test_anthropic_adapter.py`
  - `tests/agent/test_auxiliary_client.py`
  - `tests/run_agent/test_provider_fallback.py`

M1이 끝나면 실제 commit SHA와 최종 파일 목록을 기록하고, 다음 중 어떤 방식으로
`hermes/patches/anthropic-proxy-compat`를 만들지 사람이 결정한다.

1. M1의 세 commit을 provenance와 저자 정보를 보존해 새 pinned base에 rebase한다.
2. 새 upstream base에 이미 같은 변경이 있다면 patch-id와 동작 테스트를 확인하고
   `merged-upstream`으로 기록한다.
3. 일부만 upstream에 있다면 남은 동작을 최소 topic으로 다시 분리한다.

그 전까지 이 entry의 `example: true`, `enabled: false`, `commits: TBD`를 유지한다.
