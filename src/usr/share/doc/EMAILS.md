# EMAILS — Email integration via macOS Mail.app

PAI reads and writes email by observing and driving the local **macOS Mail.app**. Mail.app handles auth, IMAP/SMTP, and fetching for every account the user has configured (iCloud, Gmail, Outlook, Exchange, plain IMAP). PAI watches its on-disk state for new mail and uses AppleScript to materialize drafts.

No cloud OAuth. No GCP/Azure projects. If Mail.app can talk to it, PAI can.

## Scope

- **In:** read inbound mail (every account); mirror outbound (sends the owner makes from Mail.app, or drafts PAI wrote that the owner then sent).
- **In (v1):** PAI writes drafts. The owner reviews + sends in Mail.app manually. PAI does **not** autosend.
- **Out:** calendar, contacts, tasks, attachments-as-first-class blobs, search, labels/categories, autosend. Add later.
- **No backfill.** Bootstrap captures the current `messages.ROWID` cursor and only ingests mail that arrives after that point.

## Filesystem layout

Email lives under `home/communication/email/` (a symlink view onto `/var/spool/communication/email/`, shared across the fleet — same shape as messages).

```
home/communication/email/
    drafts/
        {name}.yaml                                                        # PAI-authored drafts (single shared dir)
    {account}/
        meta.yaml
        {YYYY-MM-DD}/
            {subject-slug}.yaml                                            # canonical
            {subject-slug}.prev -> ../{prev-date}/{prev-subject-slug}.yaml # one-hop walkback
        threads/
            {thread-slug}/
                {YYYY-MM-DD}T{HH-MM}-{subject-slug}.yaml -> ../../{date}/{subject-slug}.yaml
```

Drafts are forward-looking — the account they belong to is declared by the `from:` field on the yaml itself, not by directory placement. Received and sent mail stay per-account because the account is a fact about the message.

- `{account}` is the full email address, e.g. `me@icloud.com`. Created automatically on first sight; no `paiadd` step.
- `{YYYY-MM-DD}` is the date the message was received (or sent, for outbound) in local time.
- `{subject-slug}` is the subject sanitized: `Re:`/`Fwd:` stripped, non-alphanumerics replaced with `-`, capped at ~80 chars. Same-day collisions append `-{HH-MM}`.

## Per-message YAML

```yaml
message_id: <abc123@mail.example.com>     # RFC 5322 Message-ID — canonical identity
in_reply_to: <prev456@mail.example.com>   # parent Message-ID, if any
references:
  - <root000@mail.example.com>
  - <prev456@mail.example.com>

thread_slug: hello-world-a1b2c3d4         # {normalized-subject}-{8-char hash of root Message-ID}

from: foo@example.com
from_name: Foo Bar
to:  [me@icloud.com]
cc:  []
bcc: []

subject: Re: Hello World!
direction: inbound                        # inbound | outbound
received_at: 2026-04-30T14:32:00-07:00    # OR sent_at, depending on direction

content: |
  Body as plain text. HTML bodies get converted to text on ingest.

attachments: []                           # metadata-only for v1
provider_thread_id: "1234"                # Mail.app conversation_id; opaque
```

## Threading

Driven by RFC 5322 `Message-ID` / `In-Reply-To` / `References` — same shape regardless of provider. The thread slug is `{normalized-subject}-{8-char-hash}` of the root Message-ID; stable for the life of the thread.

Two indexes point at the same canonical files in `{date}/`:

1. **`.prev` symlink** (optional, per-message) — one-hop walkback. Skipped when the parent isn't local.
2. **`threads/{thread-slug}/` dir** — symlinks named `{received-at}-{subject-slug}.yaml` so `ls` shows the whole thread chronologically. Built incrementally; fully regenerable from yaml fields.

## Per-account meta.yaml

Auto-created on first inbound mail for an account. No manual config.

```yaml
account: me@icloud.com
provider: macmail
account_uuid: 0A836680-D0A5-4916-A3D8-24F5FC1C1204
created: 2026-04-30
```

## How it works

### Account discovery — `~/Projects/pairegistry/drivers/email/macmail/accounts.py`

Shared source of truth for inbound, outbound, and `mailsearch`. Asks Mail.app via AppleScript (`osascript`) at boot and hourly:

- `id of account` → UUID (matches the `account_uuid` parsed from `mb.url`)
- `email addresses of account` → all canonical addresses (primary + aliases like iCloud Hide-My-Email relay addresses)
- `name of inbox` / `name of sent mailbox` → **localized** mailbox names (e.g. `Gelen Kutusu` / `Gönderilmiş Öğeler` for a Turkish-locale Outlook account, `[Gmail]/Sent Mail` for Gmail)

Persisted to `home/tmp/drivers/macmail/accounts.yaml`. If AppleScript fails (Mail.app down, no automation permission), the previously-persisted config is reused — fail-soft. Public surface: `load`, `refresh`, `address_for_uuid`, `accepts_from` (case-insensitive, includes aliases), `url_like_patterns` (SQL `LIKE` patterns per (UUID, mailbox) with role tags), `role_for_url`.

Names containing diacritics are URL-encoded as Apple does — NFD-decomposed UTF-8 bytes, then `quote(safe="")` — so the patterns match the URLs Mail.app actually writes into `mailboxes.url`.

### Inbound — `~/Projects/pairegistry/drivers/email/macmail/inbound.py`

- **Index**: Mail.app maintains a SQLite index at `~/Library/Mail/V10/MailData/Envelope Index` covering all configured accounts.
- **Watcher**: kqueue VNODE on `Envelope Index-wal`. Same trick as `imessage/inbound.py` — FSEvents coalesces SQLite WAL writes, kqueue does not. A 60s ticker sits alongside as a safety net so parked rows always get retried in bounded time.
- **Cursor**: `messages.ROWID` is autoincrementing. Stored in `home/tmp/drivers/macmail/cursor.yaml`.
- **Filter**: SQL `WHERE` is built dynamically from `accounts.url_like_patterns(cfg)` — one `LIKE` per (account UUID, mailbox URL) pair, tagged inbound/outbound. No hardcoded English mailbox names; whatever Mail.app reports for `name of inbox` / `name of sent mailbox` per account is what we filter on. Sent rows produce outbound yamls — Mail.app's Sent folder is the authoritative record of "what got sent".
- **Body**: each row maps to `~/Library/Mail/V10/{account-uuid}/{Mailbox}.mbox/{store-uuid}/Data/[{ROWID//1000}/]Messages/{ROWID}.emlx`. emlx = byte-count line + RFC 5322 MIME + trailing plist; stripped and parsed with stdlib `email`. HTML bodies converted to text via `shared.html_to_text`.
- **Partial messages**: `.partial.emlx` (body not yet downloaded) caps the cursor at the parked rowid but does **not** stop the drain — later, ready rows still ingest. Idempotency by Message-ID (`shared.find_message_by_id`) ensures re-scans don't produce duplicates.
- **Account address resolution**: UUID → canonical address via `accounts.address_for_uuid(cfg, uuid)`. We never sniff `To:`/`Delivered-To:` anymore — the first inbound message often arrives addressed to a forwarder or relay alias, which would lock in the wrong identity.
- **Backlog**: catch-up at boot coalesced into a single `email:backlog` event, not N events.

### Outbound — `~/Projects/pairegistry/drivers/email/macmail/outbound.py`

- Watches `home/communication/email/drafts/*.yaml` via watchdog (single shared dir).
- PAI-facing drafting instructions live in the `drivers/email` skill
  (`name: drafting-emails`, source
  `~/Projects/pairegistry/drivers/email/SKILL.md`). PAIs normally draft
  with `bin/draft-email`, which writes the YAML file this driver watches.
- Each draft yaml carries a required `from: <email>` field naming the Mail.app account that owns the draft. Validated via `accounts.accepts_from(cfg, addr)` — accepts every address Mail.app reports for any account, **including aliases** (e.g. iCloud Hide-My-Email relay addresses). Unknown `from:` values fail fast with `draft_state: failed`.
- For each unmarked draft, builds AppleScript that calls `make new outgoing message` (with `sender:` pinned to `from:`) + `save` (NOT `send`) — the message lands in Mail.app's Drafts folder under the right account.
- Replies (`in_reply_to` set) locate the parent by Message-ID across every mailbox, use Mail's `reply` to inherit threading, then `save`. The reply window briefly opens — we close it after save (see "macOS 15 quirks" below). If the parent isn't synced yet, retries with exponential backoff (5s, 15s, 30s) before giving up.
- A malformed draft yaml (e.g. unquoted `subject: Re: foo` — the `: ` makes YAML parse it as a nested mapping) is logged and emits `email:draft_failed`, but the file is **not** rewritten — clobbering the user's content would be worse than the parse error. Email-PAI's prompt instructs it to quote any string value containing `: `.
- **Lifecycle** (`draft_state` field on the yaml): missing/`pending` → re-evaluate on next event; `pending_parent` → reply parent not found, retry pending; `drafted` → terminal success; `failed` → terminal failure (`draft_error` explains why). Boot-time scan + watchdog events both just trigger "look at this file, draft it if it has no terminal state yet".

#### Draft yaml shape

```yaml
from: me@icloud.com                          # required — picks the Mail.app account
to:  [bob@example.com]
cc:  []
subject: Re: that thing
content: |
  Hey, here's what I think.
in_reply_to: <abc@xyz>                      # optional — switches to reply path
```

When the owner eventually clicks send in Mail.app, the message hits `Sent Messages.mbox` → inbound's widened SQL filter picks it up → canonical outbound yaml lands under `{from-account}/{date}/`.

## Events

```
email:new           — one row landed (inbound or outbound mirror)
email:backlog       — boot-time coalesced summary
email:draft_failed  — AppleScript couldn't materialize a draft
```

Spec lives in `~/Projects/pairegistry/drivers/email/macmail/events.yaml`.

## Requirements

- **macOS only.** Mail.app must be configured with the accounts you want PAI to see.
- **Mail.app must be running** for new mail to appear in the index. Same constraint we already accept for Messages.app + iMessage.
- **Full Disk Access** for the kernel process — the Envelope Index lives under `~/Library/Mail/`. Same FDA toggle as iMessage.

## Why this shape

- **No cloud creds to maintain.** The previous design (Gmail API + MS Graph) needed a GCP project and Azure app registration per machine. Mail.app already has working credentials — reuse them.
- **Multi-account is free.** Single Envelope Index covers every configured account; per-account dirs fall out of the address derivation.
- **Tickless.** Every signal is an event: kqueue on the index WAL, watchdog on the drafts dir, lazy account discovery on first sight. No polling, no periodic refresh.
- **Single source of truth for sent mail.** Mail.app's Sent folder is the only writer; PAI never writes outbound yamls directly. No echo-suppression logic.
- **Human-in-the-loop on send.** v1 stops at "draft saved to Mail". Even a hallucinated recipient can't leave the machine without a click. Autosend is v2.

## macOS / Mail.app quirks worth knowing

- **`reply` no longer takes `opens window`.** macOS 15 (Sequoia) dropped the `with opens window` / `without opens window` parameter on `reply`. Earlier versions accepted it; on Sequoia it errors with `-2741: Expected end of line, etc. but found class name`. We now `reply parentMsg` bare and best-effort `close (every window whose name starts with "Re:")` after `save`. A reply window briefly flashes — acceptable cost.
- **Mailbox names are localized.** EWS Outlook in a Turkish locale stores its inbox as `Gelen Kutusu` and sent folder as `Gönderilmiş Öğeler`. Gmail uses `[Gmail]/Sent Mail`. Hardcoded English `LIKE %/INBOX` filters silently skip entire accounts. Always derive mailbox names from `name of inbox` / `name of sent mailbox` via AppleScript per account — never assume.
- **Mailbox URLs use NFD-encoded UTF-8.** Apple decomposes diacritics before percent-encoding (`Gönderilmiş` → `G%CC%88onderilmis%CC%A7`). `urllib.parse.quote(name)` on the NFC form will not match — normalize to NFD first.
- **The first inbound message lies about the account address.** Sniffing `To:` / `Delivered-To:` to derive a UUID's canonical address is unreliable: relay aliases, mailing lists, and group addresses all show up there. Use AppleScript's `email addresses of account` instead — that returns Mail's own canonical list (with the primary first), and it knows which aliases the account legitimately owns.
- **iCloud Hide-My-Email aliases are valid `from:` values.** Mail.app *can* send from a relay alias under the parent iCloud account. `accepts_from` honors every address in `email addresses of account`, not just the primary.

## Open questions (defer past v1)

- Attachments: metadata-only for v1. v2 stores blobs under `{date}/_attachments/{message-id}/`.
- Folders/labels: ignored. Inbox + Sent only.
- Search: out of scope — agent uses `grep` over the yaml tree.
- Autosend: v2. Will need an explicit per-account or per-recipient allowlist before sending without human review.
