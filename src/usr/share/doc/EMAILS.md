# EMAILS — Email integration via macOS Mail.app

PAI reads and writes email by observing and driving the local **macOS Mail.app**. Mail.app handles auth, IMAP/SMTP, and fetching for every account the user has configured (iCloud, Gmail, Outlook, Exchange, plain IMAP). PAI watches its on-disk state for new mail and uses AppleScript to materialize drafts.

No cloud OAuth. No GCP/Azure projects. If Mail.app can talk to it, PAI can.

## Scope

- **In:** read inbound mail (every account); mirror outbound (sends Arda makes from Mail.app, or drafts PAI wrote that Arda then sent).
- **In (v1):** PAI writes drafts. Arda reviews + sends in Mail.app manually. PAI does **not** autosend.
- **Out:** calendar, contacts, tasks, attachments-as-first-class blobs, search, labels/categories, autosend. Add later.
- **No backfill.** Bootstrap captures the current `messages.ROWID` cursor and only ingests mail that arrives after that point.

## Filesystem layout

Email lives under `home/communication/email/` (a symlink view onto `/var/spool/communication/email/`, shared across the fleet — same shape as messages).

```
home/communication/email/{account}/
    meta.yaml
    {YYYY-MM-DD}/
        {subject-slug}.yaml                                                # canonical
        {subject-slug}.prev -> ../{prev-date}/{prev-subject-slug}.yaml     # one-hop walkback
    threads/
        {thread-slug}/
            {YYYY-MM-DD}T{HH-MM}-{subject-slug}.yaml -> ../../{date}/{subject-slug}.yaml
    drafts/
        {name}.yaml                                                        # PAI-authored drafts
```

- `{account}` is the full email address, e.g. `arda@icloud.com`. Created automatically on first sight; no `paiadd` step.
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
account: arda@icloud.com
provider: macmail
account_uuid: 0A836680-D0A5-4916-A3D8-24F5FC1C1204
created: 2026-04-30
```

## How it works

### Inbound — `src/drivers/email/macmail/inbound.py`

- **Index**: Mail.app maintains a SQLite index at `~/Library/Mail/V10/MailData/Envelope Index` covering all configured accounts.
- **Watcher**: kqueue VNODE on `Envelope Index-wal`. Same trick as `imessage/inbound.py` — FSEvents coalesces SQLite WAL writes, kqueue does not.
- **Cursor**: `messages.ROWID` is autoincrementing. Stored in `home/tmp/drivers/macmail/cursor.yaml`.
- **Filter**: SQL pulls only INBOX + Sent mailboxes. Sent rows produce outbound yamls — Mail.app's Sent folder is the authoritative record of "what got sent", whether PAI's draft pipeline or Arda originated it.
- **Body**: each row maps to `~/Library/Mail/V10/{account-uuid}/{Mailbox}.mbox/{store-uuid}/Data/[{ROWID//1000}/]Messages/{ROWID}.emlx`. emlx = byte-count line + RFC 5322 MIME + trailing plist; stripped and parsed with stdlib `email`. HTML bodies converted to text via `shared.html_to_text`.
- **Partial messages**: `.partial.emlx` (body not yet downloaded) parks the cursor; the next WAL kick after Mail finishes the download retries. No retry timer.
- **Account discovery**: lazy. Email address is sniffed from headers (`X-Apple-Account`, `Delivered-To`, `To`, `From`) on first sight per UUID; cached in `home/tmp/drivers/macmail/accounts.yaml`.
- **Backlog**: catch-up at boot coalesced into a single `email:backlog` event, not N events.

### Outbound — `src/drivers/email/macmail/outbound.py`

- Watches `home/communication/email/{account}/drafts/*.yaml` via watchdog.
- For each unmarked draft yaml, builds AppleScript that calls `make new outgoing message` + `save` (NOT `send`) — the message lands in Mail.app's Drafts folder.
- Replies (`in_reply_to` set) use Mail's `reply` to inherit threading, then `save`.
- On success: writes `mail_app_drafted: true` + `drafted_at` back to the yaml. On failure: emits `email:draft_failed`, marks the yaml with `draft_error`.
- **Idempotent**: the marker IS the cursor. Boot-time scan + watchdog events both just trigger "look at this file, draft it if unmarked".

When Arda eventually clicks send in Mail.app, the message hits `Sent Messages.mbox` → inbound's widened SQL filter picks it up → canonical outbound yaml lands under `{date}/`.

## Events

```
email:new           — one row landed (inbound or outbound mirror)
email:backlog       — boot-time coalesced summary
email:draft_failed  — AppleScript couldn't materialize a draft
```

Spec lives in `src/drivers/email/macmail/events.yaml`.

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

## Open questions (defer past v1)

- Attachments: metadata-only for v1. v2 stores blobs under `{date}/_attachments/{message-id}/`.
- Folders/labels: ignored. Inbox + Sent only.
- Search: out of scope — agent uses `grep` over the yaml tree.
- Autosend: v2. Will need an explicit per-account or per-recipient allowlist before sending without human review.
- `.partial.emlx`: currently parks the cursor. If a partial gets stuck (e.g. Mail never finishes the download), we'd block forever. Acceptable for v1; revisit if it bites.
