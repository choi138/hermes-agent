# Versioned Hermes Deployment Configuration

This directory contains non-secret desired-state overlays for a Hermes deployment.
It is deliberately separate from `~/.hermes/config.yaml`: the live file may carry
provider endpoints, local paths, and credential references, none of which belong in
Git.

## GPT-5.6 smart routing

`gpt56-smart-routing.yaml` records the maintained routing policy:

- simple chat, planning, and citation/freshness work use `gpt-5.6-terra`;
- code implementation and multimodal work use `gpt-5.6-sol`;
- each lane uses `provider: main`, preserving the deployment's authenticated
  provider, base URL, API mode, and credentials.

Apply the scalar values to the Hermes host with its own `hermes` executable. Do
not replace the complete live config with the overlay.

```bash
hermes config set model.default gpt-5.6-sol
hermes config set smart_model_routing.enabled true
hermes config set smart_model_routing.respect_explicit_model false
hermes config set smart_model_routing.gates.repo_mutation allow
hermes config set smart_model_routing.gates.high_risk allow
hermes config set smart_model_routing.gates.gjc_escalation block

hermes config set smart_model_routing.routes.cheap_chat.provider main
hermes config set smart_model_routing.routes.cheap_chat.model gpt-5.6-terra
hermes config set smart_model_routing.routes.reasoning.provider main
hermes config set smart_model_routing.routes.reasoning.model gpt-5.6-terra
hermes config set smart_model_routing.routes.research_readonly.provider main
hermes config set smart_model_routing.routes.research_readonly.model gpt-5.6-terra
hermes config set smart_model_routing.routes.codex_implementation.provider main
hermes config set smart_model_routing.routes.codex_implementation.model gpt-5.6-sol
hermes config set smart_model_routing.routes.multimodal.provider main
hermes config set smart_model_routing.routes.multimodal.model gpt-5.6-sol
```

Start a new CLI, TUI, or gateway session after changing the configuration. Existing
agent sessions preserve their model to keep prompt caching valid.

## Maintenance checks

1. Keep this overlay synchronized with intentional changes to the deployed routing
   policy in the same Git commit.
2. Do not add API keys, bearer tokens, passwords, `base_url` values, SSH paths, or
   environment-specific provider configuration to this directory.
3. Validate routing-engine behavior with:

   ```bash
   scripts/run_tests.sh tests/test_smart_model_routing.py -q
   ```

4. On the Hermes host, exercise a new session with one prompt per lane and inspect
   the selected model in the response metadata or agent log.
