# Hermes fork patch manifest

`NousResearch/hermes-agent` 위에 유지하는 포크 런타임 패치의 인벤토리다.
관리 정책은 `DECISIONS.md`의 ADR-001을 따른다.

이 파일과 관리 스크립트는 `hermes/fork-policy`에서 관리하고,
`hermes/production`에는 직접 포함하지 않는다. `hermes/production`은 pinned base와
아래의 활성 topic branch만으로 재구성한다.

> **전환 메모(2026-08-14):** 기존 `hermes/all-work`에는 이미 148개 로컬 고유 커밋이
> 누적되어 있다. 이번 첫 적용에서는 기존 동작 보존을 우선해 `hermes/all-work` 현재 HEAD를
> 전환용 pinned base로 삼고, 새 변경(M1)부터 topic으로 관리한다. 이후 upstream sync 전에
> 기존 148개 커밋을 별도 topic 경계로 분해할지 결정한다.

## Pinned Base

- **upstream:** `NousResearch/hermes-agent`
- **base_ref:** `hermes/all-work`
- **base_commit:** `6715ceafa6c87f300900a6d58d39ae011c9c3adb`
- **pinned_at:** `2026-08-14T12:32:00+09:00`

`base_commit`은 M2 실제 적용 시점의 라이브 동일 SHA다.

```bash
git rev-parse hermes/all-work
# 6715ceafa6c87f300900a6d58d39ae011c9c3adb
```

`base_ref`는 전환용 기준이며, moving ref가 전진해도 manifest 검토 없이
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

필수 필드는 `branch`, `origin`, `upstream_pr`, `state`, `rationale`, `commits`,
`touches`다. `state` 허용값은 정확히 `local-only | pending-upstream |
merged-upstream | vendored`다.

## Patches

### 1. anthropic-proxy-compat

- **branch:** `hermes/patches/anthropic-proxy-compat`
- **origin:** `local:codex`
- **upstream_pr:** `none`
- **state:** `local-only`
- **enabled:** `true`
- **example:** `false`
- **rationale:** private Anthropic-compatible proxy(`claude.nekos.me`)를 안전하게 primary/fallback 런타임으로 사용한다. opt-in signature passthrough, OpenAI `response_format`의 Anthropic `output_config.format` 변환, fallback chain의 명시적 `api_mode` 우선순위를 각각 이식한다. `claude-fable-5` dev primary 전환과 이후 model routing 이식의 선행 호환 계층이다.
- **commits:**
  - `a57cfd80102a178eba0cf098ca237b3860456620` feat(anthropic): trust configured signature passthrough proxies
  - `f32b07af4315f24e64fefa08ca512138d23b4020` fix(aux): translate structured output for Anthropic
  - `9cb01326123ad6595fbd8ead18d40393a8189d0b` fix(fallback): honor declared API modes
- **touches:**
  - `agent/anthropic_adapter.py`
  - `agent/auxiliary_client.py`
  - `agent/chat_completion_helpers.py`
  - `agent/conversation_loop.py`
  - `hermes_cli/config.py`
  - `tests/agent/test_anthropic_signature_passthrough.py`
  - `tests/agent/test_auxiliary_client.py`
  - `tests/hermes_cli/test_provider_config_validation.py`
  - `tests/hermes_cli/test_config_validation.py`
  - `tests/run_agent/test_provider_fallback.py`
  - `tests/run_agent/test_thinking_sig_recovery_persistence.py`
