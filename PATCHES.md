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
  - `af09003c8104111f1b98c19f0298913333c0d7e8` feat(anthropic): trust configured signature passthrough proxies
  - `3f35fb0b7e101ef95728f0bd95ef79dded169469` fix(aux): translate structured output for Anthropic
  - `6eb58de2307e07a030bb4ceb772ec0d76e33fc08` fix(fallback): honor declared API modes
- **touches:**
  - `agent/anthropic_adapter.py`
  - `agent/auxiliary_client.py`
  - `agent/chat_completion_helpers.py`
  - `agent/conversation_loop.py`
  - `hermes_cli/config.py`
  - `tests/agent/test_anthropic_signature_passthrough.py`
  - `tests/agent/test_auxiliary_client.py`
  - `tests/hermes_cli/test_provider_config_validation.py`
  - `tests/run_agent/test_provider_fallback.py`
  - `tests/run_agent/test_thinking_sig_recovery_persistence.py`

### 2. model-routing

- **branch:** `hermes/patches/model-routing`
- **origin:** `cherry-pick:986ffd775cdfe88028e7cabcc9af554067569fe7`
- **upstream_pr:** `none`
- **state:** `local-only`
- **enabled:** `true`
- **example:** `false`
- **rationale:** 목적별 model route 해석, passive provider health, gateway의 fail-open shadow decision logging을 운영에 유지한다. topic 자체는 pinned base에 독립적으로 replay하고, private Anthropic proxy를 route 대상으로 쓰는 운영 구성 때문에 production에서는 `anthropic-proxy-compat` 뒤에 조립한다. upstream에 동등한 routing/health 기능이 반영되고 현재 fail-open 및 설정 호환 계약이 검증되면 제거한다.
- **commits:**
  - `6b0eaf75302b019424dd9dcf7bb4985b4ee8221f` feat(routing): add shadow model routing
  - `8b7547a2f8ec6ba23b1da2ebbfc4fcb6f3c3cfff` fix(routing): make shadow evaluation observational
- **touches:**
  - `agent/chat_completion_helpers.py`
  - `gateway/model_router.py`
  - `gateway/run.py`
  - `gateway/turn_context.py`
  - `hermes_cli/config.py`
  - `hermes_cli/config_defaults.py`
  - `hermes_cli/model_routes.py`
  - `tests/gateway/test_model_router.py`
  - `tests/hermes_cli/test_model_routes.py`
  - `tests/run_agent/test_passive_provider_health.py`

### 3. per-tool-disable

- **branch:** `hermes/patches/per-tool-disable`
- **origin:** `Soju06/hermes-agent soju/patches/per-tool-disable`
- **upstream_pr:** `none`
- **state:** `local-only`
- **enabled:** `true`
- **example:** `false`
- **rationale:** `agent.disabled_toolsets`가 toolset 이름뿐 아니라 개별 tool 이름도 제외할 수 있게 확장하고, denylist를 Codex hermes-tools MCP sidecar까지 전파해 비활성 tool이 그 경로로도 노출·호출되지 않게 한다. upstream이 동등한 tool 단위 denylist와 sidecar 전파를 제공하면 제거한다.
- **commits:**
  - `aecd117c85d10988fbec71020f2e5f91d4bfcf7c` feat(toolsets): allow disabling individual tools
- **touches:**
  - `agent/transports/hermes_tools_mcp_server.py`
  - `cli-config.yaml.example`
  - `model_tools.py`
  - `tests/agent/transports/test_hermes_tools_mcp_server.py`
  - `tests/test_model_tools.py`

### 4. strict-chat-reasoning-details

- **branch:** `hermes/patches/strict-chat-reasoning-details`
- **origin:** `Soju06/hermes-agent soju/patches/strict-chat-reasoning-details`
- **upstream_pr:** `none`
- **state:** `local-only`
- **enabled:** `true`
- **example:** `false`
- **rationale:** 엄격한 OpenAI 호환 `chat_completions` 프로바이더가 assistant replay의 `reasoning`/`reasoning_details` 필드를 400으로 거부하는 문제를 해결한다. 세션 히스토리에는 보존하고 wire payload에서만 제거한다. 현재 fallback 체인이 `anthropic_messages` 프로바이더와 codex-lb를 한 세션에서 섞기 때문에 실제 운영 경로다. upstream이 strict 엔드포인트에서 reasoning replay를 스스로 정리하면 제거한다.
- **commits:**
  - `1a880f39b8a0382afa2a8353ce1e66515decebd6` fix(chat): sanitize reasoning replay for strict providers
- **touches:**
  - `agent/transports/chat_completions.py`
  - `tests/run_agent/test_strict_api_validation.py`
