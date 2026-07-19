# send_allowlist — auto-approved recipients in ask mode

Date: 2026-07-19. Status: approved by owner.

## Problem

`capabilities.bash_exec: ask` already has an escape hatch: `bash_allowlist:`
prefix rules let trusted commands run without an approval click. The send
capabilities (`imessage_send` / `whatsapp_send` / `email_send`) have none —
in `ask` mode every send queues for owner approval, including the tenth
message today to the same trusted person.

## Shape

New top-level `send_allowlist:` map in `etc/config.yaml`, channel → list of
recipient rules. `bash_allowlist:` stays untouched.

```yaml
capabilities:
  bash_exec: ask
  imessage_send: ask
  email_send: ask

bash_allowlist:
  - git status
  - rg

send_allowlist:
  imessage:
    - "+15551234567"
  whatsapp:
    - "+15551234567"
  email:
    - premomtx@gmail.com
    - "*@mycompany.com"
```

Owner-global, like `capabilities:` — no per-PAI allowlists (YAGNI; revisit
only if asked).

## Matching (fail-closed)

New kernel module `src/boot/recipient_allowlist.py`, mirror of
`cmd_allowlist.py` in spirit: anything it can't confidently reason about
does not match, and in ask mode a non-match costs one approval click.

- Phone rules and candidates normalize to `+`-digits before comparing
  (`+1 555-123-4567` == `+15551234567`).
- **imessage**: rule matches the thread's resolved handle (phone or email)
  or the chat id — allowlisting a group chat = allowlisting its chat id.
- **whatsapp**: rule matches the resolved JID or bare phone.
- **email**: rule is an exact address (case-insensitive) or a domain
  wildcard `*@domain.com` (exact domain, no subdomains). EVERY recipient
  across to+cc+bcc must match a rule, else the send asks.
- **email replies never bypass**: Mail's scripted `reply` addresses the
  parent's From/Reply-To, which the driver cannot see at gate time — a
  draft with `in_reply_to` always asks, and the modal offers no
  always-allow rule for it (a rule derived from the draft would be a lie).
- No resolvable handle, empty allowlist, parse doubt → queue for approval.
- The allowlist is consulted ONLY in `ask` mode (moot in `yes`; nothing
  sends in `no`) — same rule as bash.

## Driver hook

In each driver's existing `mode == "ask"` branch, immediately before
`stage_pending()`: if all recipients match `config.send_allowlist(channel)`,
fall through to the direct-send path instead of staging. The kernel note on
the thread says `sent (allowlisted)` so the audit trail records why no
approval appeared.

Touch points (pairegistry first — it is upstream):

- `drivers/imessage/outbound.py` (ask branch ~line 250)
- `drivers/whatsapp/outbound.py` (ask branch, token'd .outbox hand-off)
- `drivers/email/macmail/outbound.py` (ask branch ~line 357)

Drivers already import `boot.config` for the live mode read; they gain
`config.send_allowlist(channel)` + `boot.recipient_allowlist` the same way.

## Config API (kernel, `src/boot/config.py`)

- `send_allowlist(channel, path=None) -> list[str]` — tolerant like
  `bash_allowlist()`: missing key, wrong type, unknown channel → `[]`.
- `set_send_allowlist(channel, rules, path=None) -> list[str]` — strict
  like `set_bash_allowlist()`: non-string or blank rule raises; dedupes,
  keeps order; empty list removes the channel key.

## Console

- **Approval modal**: one-click button labeled `Approve & always allow
  "{allowed_item}"` where `allowed_item` is the exact derived rule:
  - imessage/whatsapp → the recipient handle / chat id / JID, resolved
    from the thread's meta.yaml server-side (`allow_rules` rides on each
    pending record; unresolvable thread → no button)
  - email → all recipient addresses being added (comma-joined in the
    label; replies → no button)
  - bash → the full (possibly owner-edited) command as one exact prefix
    rule (deliberately narrow — the owner broadens it in the sidebar
    editor if wanted; this replaces the old two-step editable-rule flow)
  `POST /api/approve {always_allow: true}` appends the rule(s) server-side
  before flipping the record, so a raced timeout still lands the rule.
- **Backend**: `/api/send-allowlist` (add/remove per channel), mirroring
  `/api/bash-allowlist`; capability rows in the sidebar carry their
  channel's allowlist the way the bash row already does
  (`actions.py:1049`), with the same add/remove editor UI.

## Tests

- `recipient_allowlist` unit tests: phone normalization, email wildcard,
  multi-recipient email (one unmatched → no match), chat-id match, empty
  allowlist, malformed rules, fail-closed defaults.
- Config reader/writer tests alongside the `bash_allowlist` ones.
- Send-gate tests alongside `pairegistry/tests/test_outbound_send_gate.py`:
  ask + allowlisted recipient sends directly; ask + non-allowlisted stages.

## Sync discipline

Drivers edited in `~/Projects/pairegistry/` first. Kernel (`src/boot/`) and
console (`src/usr/libexec/web/`) edited here. Dual-homed bins, if touched,
synced to `pairegistry/bin/<name>/` immediately. Push both repos, then
`uv run pairelease --publish`.
