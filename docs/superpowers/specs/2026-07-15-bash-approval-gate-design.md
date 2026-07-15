# Bash approval gate — design

Date: 2026-07-15. Status: approved (owner), implementing.

## Goal

Bring PAI bash execution under the same owner approval surface as sends: a tri-state
capability (`no` / `ask` / `yes`) in the console sidebar, the same approval modal when a
command needs a decision, and an owner-editable allowlist with an "always allow" affordance
in the modal.

## Decisions (owner-confirmed)

- **Scope**: both shell tools — the one-shot `bash` tool per command, and the persistent
  PTY `shell` tool per command line sent to it. Raw stdin to an already-running approved
  program stays ungated.
- **Blocking**: the tool call holds open until decided. 10-minute timeout fails closed
  (denied). Rejection returns an error tool_result; approval runs the (possibly
  owner-edited) command.
- **Allowlist**: token-prefix rules (`git status` matches `git status -sb`, not `git push`).
  Compound commands split on `&&`, `||`, `;`, `|`; every segment must match. Commands
  containing `$(...)` or backticks never match (forced to ask). "Always allow" in the modal
  prefills the first token, editable to a longer prefix.
- **Default**: `yes` (existing installs unchanged); allowlist only consulted in `ask` mode.
- **Fleet-wide**, like other capabilities.

## Architecture

### Kernel (this repo)

- `config.py`: `bash_exec` in `CAPABILITY_SPECS` — no driver, no freeze file (enforcement is
  kernel-inline, not driver-side). `bash_allowlist()` / `set_bash_allowlist()` read/write a
  top-level `bash_allowlist:` list in `/etc/config.yaml`; hot-reloaded like send modes.
- `boot/cmd_allowlist.py` (new): pure matcher `command_allowed(command, rules) -> bool`.
- `boot/bash_gate.py` (new): stages a `channel: bash` record into the existing
  `var/spool/approvals/` spool (kernel-owned writer, no registry import) and awaits the
  record's `status` flip via a watchdog observer on the spool dir (event-driven, no
  polling). Boot sweep expires stale `pending` bash records.
- `llm.py` dispatch: gate applied in the `bash` branch and the `shell` command-input branch
  before execution. `yes` → run; `no` → error tool_result; `ask` → allowlist or block on
  approval.
- `bootstrap.py`: one terse `_CAPABILITY_LINES` entry for `bash_exec`.

### Console (this repo, `src/usr/libexec/web`)

- Backend `actions.py`: pending-list projection already channel-agnostic; `_decide()` merges
  `body_override` into `action.command` for bash records. New `/api/bash-allowlist`
  (GET list / POST add / POST remove) → config write + `kernel:reload_config`.
- Frontend: `ApprovalModal.tsx` renders bash records (monospace editable command, requesting
  PAI, Approve / Always allow… / Deny; "always allow" shows an editable prefix field then
  approves + posts the rule). `SendPermissions.tsx` gains a `bash` tri-state row + expandable
  allowlist editor.

### pairegistry

- `drivers/approvals/driver.py`: skip `channel: bash` records — the kernel delivers, the
  driver must not.

## Trade-offs accepted

- Prefix rules don't inspect redirections (`echo x > f` matches an `echo` rule).
- Cooperative gate, not a sandbox — same trust model as the send freeze.
- A blocked turn holds the PAI coroutine up to 10 min; mid-turn injection still reaches it.

## Testing

Unit: matcher (prefixes, compound splits, substitution refusal), capability projection,
allowlist config round-trip. Flow: gate coroutine resolves on spool record flip
(approve/reject/timeout), boot sweep expires orphans.
