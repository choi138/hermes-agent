# Task branch: Troubleshooting

Read this branch only for troubleshooting. Original content below is verbatim. Resolve its embedded relative paths from the skill directory (the parent of references/), as in the original SKILL.md.

## Troubleshooting

### Voice not working
1. Check `stt.enabled: true` in config.yaml
2. Verify provider: `pip install faster-whisper` or set API key
3. In gateway: `/restart`. In CLI: exit and relaunch.

### Tool not available
1. `hermes tools` — check if toolset is enabled for your platform
2. Some tools need env vars (check `.env`)
3. `/reset` after enabling tools

### Model/provider issues
1. `hermes doctor` — check config and dependencies
2. `hermes auth` — re-authenticate OAuth providers (or `hermes auth add <provider>`)
3. Check `.env` has the right API key
4. **Copilot 403**: `gh auth login` tokens do NOT work for Copilot API. You must use the Copilot-specific OAuth device code flow via `hermes model` → GitHub Copilot.

### Changes not taking effect
- **Tools/skills:** `/reset` starts a new session with updated toolset
- **Config changes:** In gateway: `/restart`. In CLI: exit and relaunch.
- **Code changes:** Restart the CLI or gateway process

### Skills not showing
1. `hermes skills list` — verify installed
2. `hermes skills config` — check platform enablement
3. Load explicitly: `/skill name` or `hermes -s name`

### Gateway issues
Check logs first:
```bash
grep -i "failed to send\|error" ~/.hermes/logs/gateway.log | tail -20
```

Common gateway problems:
- **Gateway dies on SSH logout**: Enable linger: `sudo loginctl enable-linger $USER`
- **Gateway dies on WSL2 close**: WSL2 requires `systemd=true` in `/etc/wsl.conf` for systemd services to work. Without it, gateway falls back to `nohup` (dies when session closes).
- **Gateway crash loop**: Reset the failed state: `systemctl --user reset-failed hermes-gateway`

### Platform-specific issues
- **Discord bot silent**: Must enable **Message Content Intent** in Bot → Privileged Gateway Intents.
- **Slack bot only works in DMs**: Must subscribe to `message.channels` event. Without it, the bot ignores public channels.
- **Windows-specific issues** (`Alt+Enter` newline, WinError 10106, UTF-8 BOM config, test suite, line endings): see the dedicated **Windows-Specific Quirks** section above.

### Compression failure attribution

For empty-summary/BrokenPipe incidents, correlate **all concurrent session** compression timelines before blaming the provider. Compare the compressor's intended-model warning against `Auxiliary compression: using …`, attempt telemetry, and `session_model_usage`; the warning may name the main model while the resolver still chooses the configured auxiliary model. Verify the deployed source path, not a similarly named checkout.

Two regression probes worth running in an isolated harness (no live API/config mutation): (1) fallback clears `summary_model`, the caller omits `model`, and the auxiliary resolver reselects `auxiliary.compression.model`; test the full selection boundary rather than trusting the fallback log. (2) concurrent calls reuse one cached client, and a watchdog shuts down all sockets belonging to that client; per-client ownership does not isolate concurrent requests. Test sibling-request survival, including real local sockets. Source-extracted probes with dependency stubs establish the code mechanism, not a full production replay. A timestamp correlation plus `tcp_force_closed=2` strengthens incident attribution but is not a captured socket identity trace. If the raw Responses stream was not retained, explicitly leave the empty-body origin (upstream vs parser) unresolved; model alignment is a routing simplification, not proof that transport isolation is fixed.

### Auxiliary models not working
If `auxiliary` tasks (vision, compression, session_search) fail silently, the `auto` provider can't find a backend. Either set `OPENROUTER_API_KEY` or `GOOGLE_API_KEY`, or explicitly configure each auxiliary task's provider:
```bash
hermes config set auxiliary.vision.provider <your_provider>
hermes config set auxiliary.vision.model <model_name>
```

---

