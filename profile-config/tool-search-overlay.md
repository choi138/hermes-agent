# Staged first-response tool disclosure overlay

Merge only `tools.tool_search.defer` and `tools.tool_search.listing` into the
intended profile's existing config mapping, preserving every other setting.
This is a reviewable artifact; no live config has been changed. The explicit
defer list replaces the defaults, so it includes every current default name.
If that profile already has additional deferrals, union those names after
checking that terminal/file/web/skills/execute_code/clarify/delegate and all
vault/credential safety tools remain eager. Do not replace the entire config.

Apply only with separate rollout authority, at a new session boundary. Keep
the session's catalog scope and serialized provider schemas frozen throughout
the conversation. This changes disclosure, not enabled toolsets, credentials,
privacy, approval policy, models, effort, persona, or invocation permissions.
`tool_search`, `tool_describe`, and `tool_call` remain available. The catalog
lists exact names; describe restores the full original description and argument
schema. Calls still pass production scope and argument validation. Browser
navigation/snapshot and every vault tool stay eager; less commonly needed
browser actions and media schemas are deferred until needed.

```yaml
tools:
  tool_search:
    listing: "on"
    defer:
      - computer_use
      - session_search
      - image_generate
      - todo_list
      - process_manage
      - cronjob_manage
      - drive_preview
      - gui_tour
      - desktop_preview
      - annotate_preview
      - show_tip
      - desktop_project
      - close_terminal
      - apply_layout
      - read_terminal
      - read_window_below
      - focus_pane
      - browser_click
      - browser_type
      - browser_scroll
      - browser_back
      - browser_press
      - browser_get_images
      - browser_vision
      - browser_console
      - browser_cdp
      - browser_dialog
      - browser_exec
      - vision_analyze
      - text_to_speech
      - video_analyze
      - video_generate
      - xai_video_edit
      - xai_video_extend
```

Offline verification parses this exact YAML block and exercises the real
`ToolSearchConfig.from_raw`, `assemble_tool_defs` (the production assembly
entrypoint in this snapshot), provider-schema rewrites, catalog, describe,
and `model_tools.handle_function_call` bridge path with an isolated registry.
Fixture handlers never interact with a browser, media provider, or credentials.
Undo by restoring the prior two keys at a new session boundary; do not rebuild
the tool catalog in a running conversation.
