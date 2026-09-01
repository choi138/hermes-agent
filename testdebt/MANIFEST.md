# M1 test-debt candidates (quarantined, not collected by pytest)

Recovered from A-lineage salvage refs on 2026-09-01. Every identifier in
the last column returned zero hits across the live tree at `041b1bbab5`,
so the coverage gap is real — but a gap is not automatically a port.

**Verify before promoting any file.** The A lineage stopped at 2026-08-19 and
the live lineage was rebased forward past it, so some of these assert
behavior that was deliberately replaced. Two confirmed examples:

- `test_hermes_state.py::test_writable_close_retains_truncate_checkpoint`
  contradicts the live `test_writable_close_uses_passive_checkpoint`.
- `tests/tools/test_mcp_*` cover `_strip_auth_on_cross_origin_redirect`,
  which the live tree superseded with `_make_redirect_header_stripper` plus
  `_enforce_mcp_cross_origin_redirect_boundary` (fails closed instead).

Promote a file by porting its *cases* onto the live API, never by copying
the file over the live one.

| source ref | test path | missing ids | ids checked | live bytes | staged bytes |
|---|---|---:|---:|---:|---:|
| `salvage/stash1-20260901` | `tests/hermes_cli/test_kanban_cli.py` | 39 | 40 | 6057 | 20665 |
| `salvage/p1-context-foundation-direct-20260901` | `tests/agent/test_memory_provider.py` | 30 | 40 | 58578 | 67967 |
| `salvage/stash0-20260901` | `tests/plugins/test_kanban_dashboard_plugin.py` | 28 | 40 | 46610 | 85152 |
| `salvage/stash0-20260901` | `tests/tools/test_windows_native_support.py` | 26 | 40 | 53569 | 50182 |
| `salvage/-local-20260901` | `tests/agent/test_verification_stop.py` | 22 | 33 | 8238 | 14851 |
| `salvage/stash0-20260901` | `tests/hermes_cli/test_plugins.py` | 22 | 40 | 91033 | 102380 |
| `salvage/stash0-20260901` | `tests/hermes_cli/test_kanban_goal_mode.py` | 19 | 35 | 12464 | 15963 |
| `salvage/stash0-20260901` | `tests/hermes_cli/test_kanban_db.py` | 17 | 40 | 75273 | 209342 |
| `salvage/p1-context-foundation-direct-20260901` | `tests/tools/test_mcp_client_cert.py` | 14 | 32 | 22945 | 18532 |
| `salvage/p1-context-foundation-direct-20260901` | `tests/tools/test_mcp_preflight_content_type.py` | 14 | 27 | 13919 | 19172 |
| `salvage/stash0-20260901` | `tests/hermes_cli/test_kanban_decompose.py` | 13 | 29 | 17430 | 24643 |
| `salvage/stash0-20260901` | `tests/hermes_cli/test_kanban_decompose_db.py` | 13 | 18 | 4914 | 9387 |
| `salvage/graphiti-search-memory-facts-20260901` | `tests/plugins/memory/test_graphiti_canonical_provider.py` | 12 | 24 | 132416 | 107848 |
| `salvage/stash0-20260901` | `tests/run_agent/test_provider_fallback.py` | 12 | 22 | 44375 | 36569 |
| `salvage/stash0-20260901` | `tests/tools/test_kanban_tools.py` | 11 | 40 | 55994 | 103625 |
| `salvage/stash0-20260901` | `tests/agent/test_non_stream_stale_timeout.py` | 10 | 13 | 7287 | 10096 |
| `salvage/p2-m1-classification-contract-20260901` | `tests/agent/test_direct_agent_classification.py` | 9 | 11 | 11872 | 1704 |
| `salvage/-local-20260901` | `tests/hermes_cli/test_tui_resume_flow.py` | 5 | 40 | 10330 | 61520 |
| `salvage/stash0-20260901` | `tests/tools/test_delegate.py` | 5 | 40 | 143090 | 174517 |
| `salvage/stash0-20260901` | `tests/test_transform_tool_result_hook.py` | 4 | 6 | 6865 | 7997 |
| `salvage/-local-20260901` | `tests/run_agent/test_verification_continuation_budget.py` | 3 | 12 | 11154 | 14365 |
| `salvage/latency-quality-20260901` | `tests/test_hermes_state.py` | 3 | 3 | 199151 | 169921 |
| `salvage/latency-quality-20260901` | `tests/run_agent/test_review_prompt_class_first.py` | 1 | 1 | 7693 | 5745 |
| `salvage/latency-quality-20260901` | `tests/tools/test_process_registry.py` | 1 | 2 | 113819 | 67148 |
| `salvage/stash0-20260901` | `tests/run_agent/test_run_agent.py` | 1 | 40 | 287407 | 338066 |

## Missing identifiers per file

### `tests/hermes_cli/test_kanban_cli.py`

- source: `salvage/stash1-20260901`
- missing 39/40: `remember to include performance section`, `performance section`, `Spec: rough idea`, `test_parse_workspace_flag_valid`, `test_parse_workspace_flag_expands_user`

### `tests/agent/test_memory_provider.py`

- source: `salvage/p1-context-foundation-direct-20260901`
- missing 30/40: `Block from builtin`, `Block from external`, `on_memory_write fires for`, `legacy provider fact`, `test_builtin_plus_external`

### `tests/plugins/test_kanban_dashboard_plugin.py`

- source: `salvage/stash0-20260901`
- missing 28/40: `hermes_cli.kanban._check_dispatcher_presence`, `tested on rate limiter`, `shipped the thing`, `simulated future task_age bug`, `hermes_cli.kanban_db.task_age`

### `tests/tools/test_windows_native_support.py`

- source: `salvage/stash0-20260901`
- missing 26/40: `zzz-definitely-not-on-path-xyzzy`, `C:/base/pythonw.exe`, `test_windows_path_sets_env_and_reconfigures_streams`, `fake_reconfigure`, `fake_flip`

### `tests/agent/test_verification_stop.py`

- source: `salvage/-local-20260901`
- missing 22/33: `expected 1 got 2`, `test_verify_on_stop_default_is_auto`, `test_verify_on_stop_default_auto_off_on_messaging`, `test_verify_on_stop_missing_agent_section_uses_auto`, `test_verify_on_stop_auto_sentinel_resolves_to_surface_default`

### `tests/hermes_cli/test_plugins.py`

- source: `salvage/stash0-20260901`
- missing 22/40: `new_override_plugin`, `brand_new_override_tool`, `sneaky_override_plugin`, `hermes_cli.plugins._plugin_manager`, `memory from plugin`

### `tests/hermes_cli/test_kanban_goal_mode.py`

- source: `salvage/stash0-20260901`
- missing 19/35: `test_goal_mode_defaults_off`, `test_goal_mode_persists`, `test_spawn_sets_goal_env_only_when_enabled`, `test_spawn_no_goal_env_for_plain_task`, `test_loop_continues_then_worker_completes`

### `tests/hermes_cli/test_kanban_db.py`

- source: `salvage/stash0-20260901`
- missing 17/40: `hermes_cli.kanban_db.os.waitpid`, `fake_waitpid`, `hermes_cli.kanban_db._record_worker_exit`, `worktree-rerun-board`, `wt-default-board`

### `tests/tools/test_mcp_client_cert.py`

- source: `salvage/p1-context-foundation-direct-20260901`
- missing 14/32: `test_string_cert_with_separate_key`, `test_list_form_with_passphrase`, `test_tilde_expansion`, `test_missing_key_file_raises`, `test_list_with_bad_length_raises`

### `tests/tools/test_mcp_preflight_content_type.py`

- source: `salvage/p1-context-foundation-direct-20260901`
- missing 14/27: `test_non_mcp_error_is_non_retryable_connection_error`, `test_non_2xx_responses_pass`, `test_network_error_passes`, `test_head_405_falls_back_to_get_and_rejects_html`, `test_head_501_falls_back_to_get_and_passes_json`

### `tests/hermes_cli/test_kanban_decompose.py`

- source: `salvage/stash0-20260901`
- missing 13/29: `test_decompose_fanout_false_assigns_default_when_unassigned`, `test_decompose_fanout_false_preserves_existing_assignee`, `test_decompose_fanout_false_uses_valid_llm_assignee`, `test_decompose_unknown_assignee_falls_back_to_default`, `test_decompose_handles_malformed_llm_json`

### `tests/hermes_cli/test_kanban_decompose_db.py`

- source: `salvage/stash0-20260901`
- missing 13/18: `test_decompose_returns_none_when_task_missing`, `test_decompose_returns_none_when_task_not_in_triage`, `test_decompose_empty_children_returns_none`, `test_decompose_rejects_self_parent`, `test_decompose_rejects_out_of_range_parent`

### `tests/plugins/memory/test_graphiti_canonical_provider.py`

- source: `salvage/graphiti-search-memory-facts-20260901`
- missing 12/24: `test_model_search_tool_reports_filtered_candidates_without_allowing_fallback`, `test_prefetch_allows_fallback_only_for_confirmed_empty_graphiti_result`, `test_prefetch_does_not_treat_application_error_as_missing_information`, `test_prefetch_timeout_blocks_fallback_instead_of_looking_empty`, `test_recall_reports_timeout_without_allowing_fallback`

### `tests/run_agent/test_provider_fallback.py`

- source: `salvage/stash0-20260901`
- missing 12/22: `test_single_dict_backwards_compat`, `test_list_of_providers`, `test_second_fallback_works`, `test_all_exhausted_returns_false`, `test_anthropic_host_custom_provider_uses_anthropic_messages`

### `tests/tools/test_kanban_tools.py`

- source: `salvage/stash0-20260901`
- missing 11/40: `credential is unavailable`, `finished with structured evidence`, `retry-safe child`, `Fix provider timeout`, `test_kanban_tools_visible_with_env_var`

### `tests/agent/test_non_stream_stale_timeout.py`

- source: `salvage/stash0-20260901`
- missing 10/13: `test_estimator_chat_completions_messages`, `test_estimator_responses_api_long_session_triggers_tier`, `test_estimator_bare_list_back_compat`, `test_estimator_unknown_dict_fallback`, `test_short_codex_request_uses_base_only`

### `tests/agent/test_direct_agent_classification.py`

- source: `salvage/p2-m1-classification-contract-20260901`
- missing 9/11: `_valid_payload`, `test_valid_round_trip_and_json_schema_are_stable`, `Contract tests for direct-agent request classification.`, `Add a strict classifier contract`, `side_effect_categories`

### `tests/hermes_cli/test_tui_resume_flow.py`

- source: `salvage/-local-20260901`
- missing 5/40: `fake_run_oneshot`, `fake_resolve_last`, `run_oneshot itself blew up`, `oneshot import blew up`, `hermes_cli.oneshot._run_agent`

### `tests/tools/test_delegate.py`

- source: `salvage/stash0-20260901`
- missing 5/40: `https://endpoint-a.example.com/v1`, `Error: command not found`, `parent_custom_a_pool`, `https://bedrock-runtime.us-west-2.amazonaws.com`, `https://endpoint-b.example.com/v1`

### `tests/test_transform_tool_result_hook.py`

- source: `salvage/stash0-20260901`
- missing 4/6: `test_result_unchanged_when_no_hook_registered`, `test_result_unchanged_for_none_hook_return`, `test_result_ignores_non_string_hook_returns`, `test_hook_exception_falls_back_to_original`

### `tests/run_agent/test_verification_continuation_budget.py`

- source: `salvage/-local-20260901`
- missing 3/12: `test_verification_false_finalizes_candidate_once`, `test_streamed_verification_candidate_reused_marked_previewed`, `verify check crashed`

### `tests/test_hermes_state.py`

- source: `salvage/latency-quality-20260901`
- missing 3/3: `test_writable_close_retains_truncate_checkpoint`, `test_write_path_merges_fts_only_at_cadence_boundary`, `Routine writes use bounded merge and never full optimize.`

### `tests/run_agent/test_review_prompt_class_first.py`

- source: `salvage/latency-quality-20260901`
- missing 1/1: `Memory half must still cover user facts and preferences.`

### `tests/tools/test_process_registry.py`

- source: `salvage/latency-quality-20260901`
- missing 1/2: `tools.process_registry._IS_WINDOWS`

### `tests/run_agent/test_run_agent.py`

- source: `salvage/stash0-20260901`
- missing 1/40: `request_rewritten`

