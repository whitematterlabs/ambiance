---
name: reply-to-email
description: Use when an `email:new` event arrives and you've decided this email warrants a reply, or when fixing a `draft_failed` yaml. Covers the draft yaml shape, where it goes, and the draft lifecycle.
---

# Draft an email reply

## When to use

- `email:new` arrived, you've decided this email warrants a reply, and
  you're about to write the draft yaml.
- `email:draft_failed` arrived with a fixable error and you're patching
  the yaml.

Triage criteria (don't-reply / reply / surface-to-Arda) live in the
email-pai prompt — this skill is the *how*, not the *whether*.

## Where drafts live

```
communication/email/drafts/{name}.yaml
```

**Single shared dir, NOT per-account.** The `from:` field on the yaml
picks which Mail.app account sends it. Pick a descriptive `{name}` like
`re-lunch-thursday` or `re-mom-grocery-list` — it's just a filename, not
exposed anywhere else. Same name twice = overwrite, so be specific.

## Draft yaml format

```yaml
from: arda@example.com               # required — must match a Mail.app account
to:  [recipient@example.com]
cc:  []
bcc: []
subject: Re: Original Subject
in_reply_to: <message-id-of-parent>  # required for replies
references:                          # parent's references + parent's message_id
  - <root@example.com>
  - <message-id-of-parent>
content: |
  Plain text body. Multi-paragraph is fine.

  Don't add a signature — Mail.app appends Arda's automatically.
```

For a brand-new outbound (not a reply): omit `in_reply_to` and
`references`. The driver builds a fresh outgoing message instead of
threading off a parent.

## Reading the parent

The `email:new` payload carries `path` (relative to PAI_ROOT). Read it:

```bash
cat <path>
```

Then:
- Copy parent's `message_id` → your `in_reply_to`.
- Copy parent's `references` and append parent's `message_id` → your
  `references`.
- Subject: prepend `Re: ` if not already there.

The driver uses Mail.app's `reply` to inherit threading, but supplying
these fields keeps the canonical yaml correct even if Mail's lookup fails.

## Pulling in older context with `mailsearch`

The canonical tree only holds mail that arrived after macmail-in started.
For older threads — anything Arda mentions like "the email Bob sent in
August" or "the contract thread from last quarter" — use `mailsearch` to
query Mail.app's full index and materialize hits into the canonical tree.

```bash
mailsearch --from bob@example.com --limit 5
mailsearch --subject "Q3 budget" --since 2025-01-01
mailsearch --to arda@icloud.com --account arda@icloud.com --since 2025-08-01
```

At least one of `--from`, `--to`, `--subject`, `--since` is required.
Output is a YAML list of `{path, date, account, from, subject}` sorted
newest-first. Each hit is materialized at `path` (idempotent — re-running
on the same hit is a no-op) and ready to `cat` or `grep`.

**When to reach for it:**
- Drafting a reply that references a prior conversation you can't find on
  disk.
- Answering an Arda nudge that names a sender, subject, or rough date.
- Reconstructing a thread the cursor missed because it predates the
  driver.

**When not to:**
- The relevant message is already in the event payload's `path` — just
  `cat` it.
- You're tempted to use it as a way to "browse" — pick a tight filter
  and small `--limit`. The default cap is 20; max is 200.

`mailsearch --help` lists every flag.

## Picking `from:`

Use the **account dir the parent message lives in**:
`communication/email/<account>/...` — that `<account>` is your `from:`.

Do **not** read the parent's `to:` header. It often contains a
Hide-My-Email relay alias, a mailing list, or a forwarder, which Mail.app
will reject as a sender even though it can deliver mail to it.

The account dir is the canonical address Mail.app reports for that
account (derived via AppleScript, not header-sniffed), so it's always a
valid `from:`. The driver also accepts known aliases of an account
(e.g. iCloud Hide-My-Email addresses) — but when in doubt, just use the
account dir name.

`from:` must match an address Mail.app knows about. The driver validates
at boot and rejects unknown addresses with a clean `draft_error`.

## YAML gotcha: quote strings with `: `

YAML treats `: ` (colon-space) inside an unquoted value as a nested
mapping. A subject like `Re: Foo` written as:

```yaml
subject: Re: Foo          # BROKEN — parser error, draft silently stalls
```

…will fail to parse. Always quote:

```yaml
subject: "Re: Foo"        # correct
```

Same rule for any value containing `: `, `#`, leading `-`, or starting
with `[` / `{`. When in doubt, quote.

## Lifecycle (read-only)

After you write the yaml, the macmail-out driver stamps it:

- `draft_state: drafted` + `drafted_at` — Mail.app accepted it. Done.
- `draft_state: pending_parent` + `draft_retries: N` — reply parent not
  synced yet; driver retries with backoff (5s, 15s, 30s). Wait, don't
  retry yourself.
- `draft_state: failed` + `draft_error` — terminal. An
  `email:draft_failed` event will fire — handle per the email-pai prompt.

You never set these fields. Treat them as read-only from your side.

## Verification

After a successful draft:
1. `cat communication/email/drafts/{name}.yaml` — `draft_state: drafted`.
2. The draft appears in Mail.app's Drafts folder under the `from:` account.

When Arda clicks Send, the message hits Sent → macmail-in ingests it as
an outbound canonical yaml under `{from-account}/{date}/`. The draft yaml
in `drafts/` stays put as the historical record of what you wrote.

## Boundaries

- Drafts only — never `send`. v1 is human-in-the-loop on every outbound.
- Don't write replies to newsletters, no-reply addresses, automated mail,
  or unknown senders without surfacing first. The triage decision belongs
  upstream of this skill.
- Don't follow up on your own drafts. One draft per inbound message.

## Read these next

- `memory/doc/EMAILS.md` — full email subsystem doc (driver internals,
  filesystem layout, threading semantics).
- `events.yaml` for the macmail driver — authoritative event payload schema.
