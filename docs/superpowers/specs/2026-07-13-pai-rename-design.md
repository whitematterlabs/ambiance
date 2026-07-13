# PAI instance rename (display name) — design

2026-07-13

## Goal

The owner can rename any fleet PAI from the web console: a pencil icon next to
the name in the chat pane header opens an inline edit. The chosen name also
flows into the PAI's system prompt identity line ("You are <name> …").

## Decision: display name, not slug rename

The slug anchors `/home/<slug>`, `/proc/<slug>`, `/var/lib/instances/<slug>`,
event routing, and the pid invariant. Renaming it would be destructive churn.
Instead a new optional fleet-entry field `display_name:` in `/etc/config.yaml`
carries the human-facing name; the slug stays stable. Empty/absent
`display_name` means "use the slug" everywhere (today's behavior, unchanged).

## Data flow

`/etc/config.yaml` `pais[].display_name` (source of truth)
→ `CONFIG_MANAGED_FIELDS` → reconcile writes it into `/proc/<slug>/spec.yaml`
→ `nudge.py` passes it to `bootstrap.build_system_prompt`
→ `_pai_line` renders `You are <display_name> — PAI pid N. …` inside
  `<pai-instance>`.
→ web hub `read_fleet()` sets `title` from spec `display_name` (fallback slug);
  the existing `title || slug` rendering in FleetTabs / MobileMenu / chat
  header picks it up with no further frontend plumbing.

## Changes

Kernel (`src/boot/`):
- `config.py`: add `display_name` to `CONFIG_MANAGED_FIELDS`; validate it as a
  string in `_validate_pai_entry`; new `set_pai_display_name(name, value)`
  mirroring `set_pai_model` (atomic tmp+rename; empty value pops the field).
- `bootstrap.py`: `build_system_prompt(display_name=…)` →
  `_runtime_blocks` → `_pai_line` renders the name when present.
- `nudge.py`: pass `pai_spec.get("display_name")`.
- `src/bin/paiclone.py`: clones pop `display_name` (same reasoning as
  `fallback`/`wake_on` — a clone must not silently impersonate its source).

Web backend (`src/usr/libexec/web/pai_web/`):
- `hub.py read_fleet`: `title = display_name or slug`.
- `actions.py set_pai_display_name`: config write under `_config_write_lock`,
  then `kernel:reload_config` (action `rename`) so reconcile projects the spec
  and the next turn's prompt uses the new name.
- `server.py`: `POST /api/rename {pai, display_name}` → 400 on unknown pai.

Frontend (`src/usr/libexec/web/src/`):
- `api.ts`: `renamePai(pai, displayName)`.
- `App.tsx`: chat header title becomes inline-editable — pencil button next to
  `chat-title` (hidden for subagents); click swaps in an input pre-filled with
  the current title; Enter/blur saves (optimistic fleet-state update + POST),
  Esc cancels. Save failure reverts and surfaces the error in the status bar.
- `styles.css`: pencil + inline input styling.

## Error handling

- Unknown PAI / non-fleet target → 400 from the backend, UI reverts.
- Kernel down: config.yaml is updated; spec/prompt catch up on next kernel
  boot reconcile (same semantics as the model picker).
- Whitespace-only name = clear → falls back to slug.

## Testing

- `set_pai_display_name` round-trip incl. clearing and unknown-pai error.
- `_validate_pai_entry` accepts string / rejects non-string display_name.
- reconcile projects display_name into spec.yaml.
- `build_system_prompt` renders the name in `<pai-instance>`; absent → old line.
- paiclone drops display_name.
