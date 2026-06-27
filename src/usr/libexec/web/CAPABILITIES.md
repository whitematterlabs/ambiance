# PAI web GUI — capability parity with the TUI

The web GUI is an **owner surface**. Architecture invariant:

```
TUI / GUI / other owner surface  <->  kernel  <->  LLM
```

The surface *attaches* to a running kernel. It never owns the kernel or its
runtime: it only **reads** the on-disk FHS state and **writes** the same two
things the TUI writes — a line appended to a me-thread day-file, and an event
file dropped into `run/pai/events/`. Everything else (spawning PAIs, routing,
nudging, busy state) belongs to the kernel.

This file enumerates every TUI capability (`src/sbin/tui/`) so the web GUI can
match it feature-for-feature.

## Layout

| TUI region | Source | Web component |
|---|---|---|
| Header with live clock + title | `Header(show_clock=True)` | `Header` |
| Chat column (2fr) | `#chat-col` | left column |
| Side column (1fr): procs / activity / events / log | `#side-col` | right column |
| Status bar (1 line) | `#status` | `StatusBar` |
| Message input | `#input` | `MessageInput` |

## Capabilities

### 1. Fleet tabs (one per running PAI)
- One tab per **running** `kind:pai` proc (`status == running`). Title `{slug} #{pid}`.
  Source: `_discover_pai_pids`, `_add_pai_tab`.
- Tabs reconcile live as PAIs start/stop (added/removed on every `/proc` change).
- Default active tab = the **fallback** PAI (owner-facing), not lowest pid.
  Source: `_fallback_pid`.
- Resolved subagents (no longer running) never get tabs.

### 2. Chat pane (per PAI)
- Renders **today's** `me/{pid}/YYYY-MM-DD.md` thread, live on file change.
  Source: `MeThreadWatcher`, `ChatPane.render_snapshot`.
- Messages are split on `^[HH:MM] sender:` headers; bodies may span multiple
  lines (multi-line markdown survives). Source: `_MSG_HEADER`, `MeSnapshot`.
- Per-speaker header styling: `me` → green, `pai` → magenta, `[kernel…` → dim,
  anything else → cyan. Source: `_style_message`.
- Body rendered as **markdown** (headings/lists/code fences).
- Tool/thinking lines whose body starts with `» ` render as italic dim cyan,
  **not** markdown. Source: `_style_message`.
- Auto-scrolls to the newest message.

### 3. Send a message
- Enter sends. Appends `[HH:MM] me: <text>` to today's day-file, then emits a
  `new_message` event (`source: tui`/`web`, `thread: me`, `target_pid`, `text`).
  Source: `on_input_submitted`. **This is the only write besides the event.**

### 4. Run a shell command (`!cmd`)
- Input starting with `!` runs a shell command with PAI's PATH
  (`bin:usr/bin` prepended), `cwd = home_for(slug)`, env `PAI_SLUG`/`PAI_ROOT`.
  Source: `_run_shell`.
- Output streams into the chat pane (transient, **not** written to the thread):
  the `$ cmd` echo, stdout/stderr lines, then `exit N`.
- On `rc == 0`, applies any queued clear/compact action
  (`apply_pending_history_action`) and zeroes the proc's ctx cell.

### 5. Interrupt
- Esc emits `{source, kind: interrupt, pai: <pid>}` to the active PAI.
  Source: `action_interrupt`.

### 6. Tab navigation (keyboard)
- `Ctrl+Tab` / `Ctrl+Shift+Tab` → next / prev tab (`action_next_tab/prev_tab`).
- `Ctrl+1..9` → select tab N (`action_select_tab`).

### 7. Provider switching (command palette)
- Command palette entries `Provider: Anthropic` / `Provider: Deepseek`, with
  help text `active` / `switch on next turn`. Writes
  `memory/myself/provider.yaml`. Source: `ProviderCommands`, `set_provider`.

### 8. Running procs list
- Table columns: `slug, pid, type, parent, ctx, when`. Source: `ProcList`.
- Tree-ordered (roots first, subagents indented with box-drawing prefixes).
  Source: `order_as_tree`, `tree_prefix`.
- `type` inferred: pai / subagent:<name> / driver / cron / timer / service / deadline.
  Source: `_infer_type`.
- `ctx` = last LLM prompt-window tokens, compact-formatted (`12.3k`/`187k`/`1.2M`).
  Source: `_read_ctx_tokens`, `_fmt_ctx`.
- `when` = deadline (trimmed to `MM-DD HH:MM`) or cron schedule. Source: `_short_when`.
- Live updates on every `/proc` change.

### 9. Status bar
- Reflects the **active** PAI's busy state: `{slug}: {reason} ({elapsed}s)` if
  `/proc/<slug>/busy` exists, else `idle`. Source: `_format_busy`, `_refresh_status`.
- Also shows transient action feedback: `sent → pid N, waiting…`,
  `interrupt sent → pid N, cancelled`, `shell: …`.

### 10. PAI activity pane
- Live, parsed view of `kernel.log`: nudges (`> …`, `done.`, `! failed`), each
  PAI shell command (`[tag] $ cmd`) with `ok/fail (exit N)`, and PAI output
  lines (`pai:<pid>: …`). Command output is elided to ~2 lines + `…`.
  Supervisor banner lines dim. Source: `PaiActivity.ingest`.

### 11. Events strip
- Live feed of new files in `run/pai/events/`: `HH:MM:SS source:kind → target`.
  Recovers consumed files from the filename. Source: `EventStrip.write_sighting`,
  `EventsWatcher`.

### 12. Kernel log tail
- Tails `var/log/kernel/kernel.log` from EOF (only new lines). `[kernel]` prefix
  dim-cyan, `[pai…]` prefix bold magenta. Source: `LogTail`, `LogTailer`.

## Non-goals (kept on the kernel side)
- No spawning/stopping PAIs, no routing, no nudging, no busy bookkeeping.
- The surface does not poll the kernel for liveness; it reacts to FS events
  (watchdog), matching PAI's tickless central dogma.
</content>
</invoke>
