# Production memory and restart-resume parity map

Source of truth for this comparison: production commit
`9502b418bd29a3ddf56933fe52e3c2cfe33b42be` versus the
`reintegrate/v2026.9.21-20260923` worktree. This is a source-code and focused
test map, not evidence that any path has passed a live Discord turn.

| Contract | Production writer / reader | Release integration seam | Current status |
| --- | --- | --- | --- |
| Two-step, grounded declarative note write and read | `tools/notes_tool.py` calls `agent/memory_pipeline.py` and `agent/notes_store.py`; `agent/memory_taint.py` rejects memory-derived citations | Register the three names in the `memory` toolset, route both sequential and concurrent agent calls through `agent/inline_tool_executors.py`, and preserve prompt guidance in `agent/system_prompt.py` | Ported; Notes wiring and related tests passed 207 cases with 1 skip. Live Discord write/read is unproved. |
| Durable candidate proposals and evidence | `memory_propose` appends through `agent/memory_journal.py::PendingTurnWAL`; `MemoryManager.sync_all` journals and acknowledges completed turns; `L0Mirror` records evidence | Integrate journaling into this release's `agent/memory_manager.py::sync_all` without losing its `turn_author`, profile scope, single-worker ordering, or shutdown drain | Ported; focused WAL/L0, failure-recording, profile A→B→A, and boundary tests passed 174 cases. Production write behavior is unproved. |
| Background curator and optional graph backfill | `agent/ingest_curator.py` observes completed, pre-compress, and session-end turns; `MemoryManager.sync_note_backfill` and `sync_curated_episode` use the same write worker | Adapt to `agent/turn_finalizer.py`, compression/session-end hooks, config defaults, and external provider metadata contract | Missing; production defaults keep curator ingest off and shadow mode on, so no new graph writes should be enabled by porting |
| Same-turn restart continuation | `agent/turn_resume.py` normalizes the persisted tail; production loop uses `resume_turn=True`; Gateway consumes `resume_pending` without fabricating a new user turn | Adapt `agent/conversation_loop.py`, `agent/turn_context.py`, `agent/turn_facade.py`, and `gateway/run_turn_runner.py`/startup scheduler to this release's split phases | Ported; same-turn resume and delivery-focused tests passed 78 cases. A live interrupted turn and restart are unproved. |
| External Graphiti provider | Production provider reads Graphiti and accepts write metadata; staged plugin was checked against standalone MCP discovery and prefetch | Exercise the installed provider through the integrated `MemoryManager` in isolated homes, then verify Discord canary only after all gates | Isolated installed-plugin and integrated `MemoryManager` read-only recall passed; one live Graphiti response arrived in 1.76 seconds. No production installation, live Discord turn, or graph write has been proved. |

The journal/taint/pipeline/tool contract, agent-loop wiring, same-turn resume,
and isolated A→B→A profile checks are complete at the focused-test level. The
remaining gates are the final full Gateway rerun, four-profile backup/restore
proof, reviewed commit, and controlled single-gateway Discord canary. Preserve
the existing code/config/state and rollback path before touching the live bot.
No production service, token, or profile is used by local tests.
