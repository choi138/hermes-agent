# Hermes fork patch manifest

`NousResearch/hermes-agent` 위에 유지하는 포크 런타임 패치의 인벤토리다.
관리 정책은 `DECISIONS.md`의 ADR-001을 따른다.

이 파일과 관리 스크립트는 `hermes/fork-policy`에서 관리하고,
`hermes/production`에는 직접 포함하지 않는다. `hermes/production`은 pinned base와
아래의 활성 topic branch만으로 재구성한다.

> **전환 메모(2026-08-20):** 레거시 포크 이력은
> `upstream/main@4511ba49dd5830062ffbcfbdb3f2a4fc7f278ccb` 위의 단일 squash
> integration commit으로 통합했다. 그 통합과 후속 결함 수정을 포함하는
> `hermes/patches/legacy-integration`을 새 pinned base로 삼는다. 기존에 별도 패치로
> 기록했던 6개 topic은 모두 이 기준점 안에 포함되므로 더 이상 독립 entry로 관리하지 않는다.

> **은퇴 메모(2026-09-01):** ADR-003으로 패치 스택을 은퇴시켰다. `hermes/production`과
> `hermes/patches/*`는 삭제됐고 이 매니페스트가 가리키는 브랜치는 더 이상 존재하지
> 않으므로 `bin/hermes-patches`는 동작하지 않는다. 아래 내용은 이력으로만 남긴다.
> 활성 패치 3개와 pinned base는 모두 `hermes/all-work`의 조상이므로 내용은 보존돼 있다.

## Pinned Base

- **upstream:** `NousResearch/hermes-agent`
- **base_ref:** `hermes/patches/legacy-integration`
- **base_commit:** `fd6e8fb67bb0588708cb2979a53437cdfd2e3b5c`
- **pinned_at:** `2026-08-20T14:30:00+09:00`

`base_commit`은 레거시 통합과 그 후속 결함 수정을 함께 고정한 exact SHA다.

```bash
git rev-parse hermes/patches/legacy-integration
# fd6e8fb67bb0588708cb2979a53437cdfd2e3b5c
```

`base_ref`는 통합 기준이며, moving ref가 전진해도 manifest 검토 없이
`base_commit`을 바꾸지 않는다.

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

필수 필드는 `branch`, `origin`, `upstream_pr`, `state`, `enabled`, `example`,
`rationale`, `commits`, `touches`다. `state` 허용값은 정확히 `local-only |
pending-upstream | merged-upstream | vendored`다.

## Patches

### 1. mood-routing

- **branch:** `hermes/patches/mood-routing`
- **origin:** `local:codex`
- **upstream_pr:** `none`
- **state:** `local-only`
- **enabled:** `true`
- **example:** `false`
- **rationale:** `model_routes.moods` 설정을 바탕으로 매 turn의 mood classification field를 만들고, 분류 결과에 맞는 tone을 gateway system prompt에 주입한다. 요청마다 말투와 persona를 명시적으로 조정해야 하는 운영 경로를 보존하며, upstream이 동등한 per-turn tone/persona routing hook을 제공하면 제거한다.
- **commits:**
  - `313f313dd095b276af26223e08a7d948a8da7ddd` feat(gateway): mood classification shadow field (M1)
  - `94e7c0caf4fcd8b5bc9a74273f2f969714bbdc7d` feat(config): parse model_routes.moods placeholder (M1)
  - `009775a1670c59f6785d504fcbc1f8afbb5a6215` feat(gateway): mood tone injection (M2)
- **touches:**
  - `gateway/model_router.py`
  - `gateway/mood_loader.py`
  - `gateway/run.py`
  - `hermes_cli/model_routes.py`
  - `tests/gateway/test_model_router.py`
  - `tests/gateway/test_mood_injection.py`
  - `tests/hermes_cli/test_model_routes.py`

### 2. mention-inbox-multi-repo

- **branch:** `hermes/patches/mention-inbox-multi-repo`
- **origin:** `local:codex`
- **upstream_pr:** `none`
- **state:** `local-only`
- **enabled:** `true`
- **example:** `false`
- **rationale:** mention-inbox plugin의 단일 신뢰 저장소 제한을 명시적인 다중 저장소 allowlist로 확장한다. trust gate를 비활성화하지 않고도 여러 GitHub 저장소의 mention intake를 안전하게 처리해야 하는 운영 요구를 충족하며, upstream이 동등한 다중 저장소 신뢰 목록을 제공하면 제거한다.
- **commits:**
  - `b4abf922a674cc5c82879626a7fe930473a393b7` feat(mention-inbox): allow a multi-repository trust allowlist
- **touches:**
  - `plugins/mention_inbox/README.md`
  - `plugins/mention_inbox/approval.py`
  - `plugins/mention_inbox/operational.py`
  - `plugins/mention_inbox/thread_session.py`
  - `tests/plugins/test_github_mention_collector.py`
  - `tests/plugins/test_mention_inbox_approval.py`
  - `tests/plugins/test_mention_inbox_delivery_thread.py`
  - `tests/plugins/test_mention_inbox_operational.py`
  - `tests/plugins/test_mention_inbox_thread_session.py`

### 3. config-set-json

- **branch:** `hermes/patches/config-set-json`
- **origin:** `local:codex`
- **upstream_pr:** `none`
- **state:** `local-only`
- **enabled:** `true`
- **example:** `false`
- **rationale:** `hermes config set`이 JSON array/object 인자를 구조화된 값이 아닌 bracket literal text로 저장해, 해당 key를 list/dict로 검증하는 consumer가 fail-closed한 뒤 기본값으로 조용히 되돌아가는 문제를 고친다. upstream이 같은 함수에 `_looks_structured_value` + `yaml.safe_load` 기반 처리 경로를 추가했고, 그 구현이 우리 `json.loads` 경로의 상위집합(JSON flow style + multi-line YAML block)임을 확인해 upstream 구현을 채택하고 우리 회귀 테스트를 남겨 그 경로를 검증한다. upstream 동작이 이 테스트로 계속 보증되면 제거를 검토한다.
- **commits:**
  - `ca1c4d055ac78679ef71f8474b78a192f6150af0` fix(cli): reconcile config set structured values with upstream
- **touches:**
  - `tests/hermes_cli/test_set_config_value.py`

## Contained in the pinned base

다음 6개 기존 topic은 그 내용이 모두 `base_commit` 안에 포함되어 있으므로 의도적으로
별도 patch entry를 두지 않는다.

- `anthropic-proxy-compat`
- `model-routing`
- `per-tool-disable`
- `strict-chat-reasoning-details`
- `refusal-chain`
- `durable-bg-processes`
