# Inline activity indicator (web GUI chat)

**Date:** 2026-05-24
**Surface:** `src/usr/libexec/web` (React frontend only)
**Status:** Approved design, ready for implementation plan

## Goal

Show what the active PAI is doing *right now* inline at the bottom of the web
GUI chat, the way Claude briefly shows its current step while it works. The
indicator is ephemeral: it appears while the PAI is busy and vanishes when the
turn ends. The transcript stays clean — only real messages persist.

## Key finding: no backend changes

The live signal already streams to the browser. The kernel writes
`proc/<slug>/busy` on every step:

- `waiting on <model>` at the start of each turn iteration (`src/boot/llm.py:209`)
- `bash: <command>` per bash command (`src/boot/llm.py:263`)
- shell-tool reasons for key sends

`busy` lives under `PROC_DIR`, which the hub watches recursively and rebroadcasts
as a `procs` SSE message within ~60ms of any change
(`src/usr/libexec/web/pai_web/hub.py:285,140`). `App` already lands this in
`activeProc.busy = { reason, started_at }`. Today that granular `reason` only
feeds the binary "Working/Ready" header chip and the side-panel Activity tab; the
chat column discards it.

This feature is therefore **pure frontend**: render `activeProc.busy.reason` as
an ephemeral line in the chat flow.

## Decisions (locked)

- **Lifecycle:** ephemeral, like Claude. Mount while busy, unmount on idle. No
  "done" summary, no persisted step history in the transcript.
- **Granularity:** current step only — one line showing the most recent action.
- **Side panel:** keep both. The right-side Activity tab is unchanged; it remains
  the fleet-wide scrollback. This is the glanceable "now".
- **Placement:** inside the chat scroll container, as the last item under the
  latest message, riding the existing sticky-bottom behavior.
- **Step text:** light humanizing — map known reasons to friendly verbs, raw
  fallback otherwise.

## Components & data flow

### `humanizeStep(reason: string): { verb: string; detail?: string }`

New pure helper (own module, e.g. `src/usr/libexec/web/src/working.ts`).
Maps `busy.reason` to display parts:

| reason pattern            | verb            | detail            |
|---------------------------|-----------------|-------------------|
| `waiting on <model>`      | `Thinking…`     | —                 |
| `bash: <cmd>`             | `Running`       | `<cmd>` (mono)    |
| `shell: <…>`              | `Sending keys`  | `<…>` (optional)  |
| anything else (non-empty) | the raw reason  | —                 |

`detail`, when present, renders in a truncated monospace span. The verb is never
blank — an unmatched non-empty reason is shown verbatim as the verb.

### `WorkingIndicator` component

New file `src/usr/libexec/web/src/components/WorkingIndicator.tsx`.

- Props: `busy: { reason: string; started_at: number }` (only rendered when
  non-null — the parent guards the mount).
- Renders one row: a CSS shimmer/pulse dot + `verb` + optional mono `detail` +
  elapsed `(Ns)`.
- Elapsed ticks once a second via a local `setInterval` that re-renders using the
  existing `elapsedSecs(busy.started_at)` helper from `status.ts`. The interval
  is cleared on unmount.
- Uses existing palette tokens / CSS variables; **no new dependencies**.

### Wiring

- `ChatPane` gains a `busy: ProcRow["busy"]` prop. When non-null it renders
  `<WorkingIndicator busy={busy} />` as the final child inside the scroll
  container (after the messages map, after the trailing shell slot).
- `ChatPane`'s sticky-bottom `useLayoutEffect` dependency array gains the busy
  signal (e.g. `busy?.reason`) so a newly-appearing or changing indicator
  re-anchors the scroll to the bottom.
- `App` passes `activeProc?.busy ?? null` into `ChatPane`. No other App changes.

## Out of scope (YAGNI)

- No "done in Ns" collapsed summary chip.
- No persisted per-step history inline.
- No removal of the header "Working/Ready" chip or the side-panel Activity tab.
- No new SSE message type, no Python changes.

## Edge cases

- **Turn ends / interrupt (Esc):** kernel clears `busy` → `procs` update →
  `activeProc.busy` becomes null → indicator unmounts. No special handling.
- **Tab switch:** the indicator reads `activeProc` for the active PAI only, so it
  shows the correct PAI's current step, or nothing if that PAI is idle.
- **Empty/unknown reason:** `busy` present but reason empty → fall back to
  `Thinking…` so the row is never blank while busy.
- **Reconnect snapshot:** a fresh `hello`/`procs` carrying a busy PAI shows the
  indicator immediately; elapsed is computed from `started_at`, so it reflects
  true elapsed even across reconnects.

## Testing

The web frontend has **no test runner** (no vitest/jest, zero existing
`.test.*` files); it relies on `tsc --noEmit` (the `build` script) for type
safety and manual verification. This feature matches that convention — no new
test infra, no new dev dependencies.

- **Typecheck:** `pnpm build` (runs `tsc --noEmit`) must pass — covers the new
  helper signature, the `ChatPane` prop, and the component.
- **Manual verification:** start a PAI turn from the web GUI and confirm the
  indicator (a) appears under the latest message while busy, (b) updates its
  step text as the PAI moves between thinking and running commands, (c) ticks the
  elapsed seconds, and (d) vanishes when the turn ends or is interrupted (Esc).
- `humanizeStep` is kept pure and small so its behavior is obvious by reading; if
  a runner is added later it is trivially unit-testable.

## Files touched

- `src/usr/libexec/web/src/working.ts` (new) — `humanizeStep`.
- `src/usr/libexec/web/src/components/WorkingIndicator.tsx` (new).
- `src/usr/libexec/web/src/components/ChatPane.tsx` — `busy` prop + render + dep.
- `src/usr/libexec/web/src/App.tsx` — pass `activeProc?.busy` to `ChatPane`.
- `src/usr/libexec/web/src/styles.css` — indicator + shimmer styles.
