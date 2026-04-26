# EMAILS — Outlook/Office Email Integration

Design for a PAI driver that lets the agent read and write email through a personal Outlook account (and any other account that speaks Microsoft Graph).

## Scope

- **In:** read inbound mail, send outbound mail (including replies).
- **Out:** calendar, contacts, tasks, OneDrive, attachments-as-first-class, search, labels/categories. Add later as separate drivers.
- **No backfill.** The driver starts from "now" — bootstrap captures the current Graph delta token and only ingests mail that arrives after that point. Existing mailbox contents are not imported.

## Filesystem layout

Email is its own root under `live/communication/`, parallel to `messages/`.

```
live/communication/email/{account}/
    meta.yaml
    {YYYY-MM-DD}/
        {subject-slug}.yaml
        {subject-slug}.prev -> ../{prev-date}/{prev-subject-slug}.yaml   (optional, one-hop)
    threads/
        {thread-slug}/
            {YYYY-MM-DD}T{HH-MM}-{subject-slug}.yaml -> ../../{date}/{subject-slug}.yaml
    drafts/
        {draft-id}.yaml
    tmp/
        delta-token, oauth tokens, etc. (or under live/tmp/drivers/email/{account}/)
```

- `{account}` is the full email address, e.g. `arda@outlook.com`. Supports multiple accounts cleanly.
- `{YYYY-MM-DD}` is the date the message was received (in local time).
- `{subject-slug}` is the subject sanitized for filesystem use: `Re:` and `Fwd:` prefixes stripped, `/` and other path-unsafe chars replaced with `-`, collapsed whitespace, capped at ~80 chars. On collisions within a day, append `-{HH-MM}`.

## Per-message YAML

```yaml
message_id: <CAFooBar123@mail.gmail.com>     # RFC 5322 Message-ID — canonical identity
in_reply_to: <CAPrev456@mail.gmail.com>      # parent Message-ID, if any
references:                                   # full ancestor chain (root → parent)
  - <CARoot000@mail.gmail.com>
  - <CAMid123@mail.gmail.com>
  - <CAPrev456@mail.gmail.com>

thread_slug: hello-world-a1b2c3d4            # derived; see "Threading" below

from: foo@gmail.com
from_name: Foo Bar
to:
  - me@outlook.com
cc: []
bcc: []

subject: Re: Hello World!
received_at: 2026-04-25T14:32:00-07:00
direction: inbound                            # inbound | outbound

content: |
  Body as plain text. HTML bodies get converted to text on ingest
  (single source of truth — we don't store both).

attachments: []                               # [{name, size, content_type, graph_id}] — metadata only for v1
```

Outbound messages use the same schema with `direction: outbound`, `from` set to the account, and a `sent_at` field instead of `received_at`.

## Threading

Each message stores its `Message-ID`, `In-Reply-To`, and `References` headers. These are the source of truth.

**Thread slug** is derived once per thread and stable for its lifetime:

- Take the root Message-ID (the first entry in `References`, or the message's own `Message-ID` if it has no parent).
- Slug = `{normalized-subject}-{8-char-hash-of-root-message-id}`.
- Normalized subject = subject with `Re:`/`Fwd:`/`RE:`/etc. stripped, lowercased, whitespace collapsed, non-alphanumerics replaced with `-`.

**Two indexes** point back at the same canonical files in `{date}/`:

1. **`.prev` symlink** (optional, per-message) — `Re Hello World.prev` → `../2026-04-23/Hello World.yaml`. Gives one-hop walkback. Skipped when the parent can't be located locally (e.g. parent was sent before bootstrap).

2. **`threads/{thread-slug}/` dir** — symlinks named `{received-at}-{subject-slug}.yaml` so `ls` shows the whole thread in chronological order. Built incrementally as messages arrive; fully regenerable from yaml fields if it ever drifts.

Both indexes are derived. The `{date}/` files are authoritative.

## Inbound driver (`src/drivers/email/inbound.py`)

- Auth: MS Graph OAuth via device-code flow. Refresh token cached at `live/tmp/drivers/email/{account}/token.json`. Public client (no secret) — works with personal outlook.com accounts (tenant = `consumers`) registered with `Mail.ReadWrite`, `Mail.Send`, `offline_access`, `User.Read`.
- Change detection: Graph **delta query** (`/me/mailFolders/Inbox/messages/delta`). Bootstrap pass on first run captures the current delta token and writes nothing. Subsequent polls (every N seconds, configurable) pull only changes since the last token.
- For each new message:
  1. Fetch full message body, convert HTML to text.
  2. Compute `thread_slug` from headers.
  3. Write `{date}/{subject-slug}.yaml`.
  4. Update `threads/{thread-slug}/` symlink.
  5. Best-effort `.prev` symlink to the parent if it exists locally.
  6. Emit a `new_email` event into `live/events/` with `{source: email, account, thread_slug, subject, from, path}`.
- Catchup: on boot, run one delta-token sync. If it returns a backlog, emit a single `email_backlog` event (mirroring imessage's coalesced backlog), not N events.

## Outbound driver (`src/drivers/email/outbound.py`)

Tails `live/communication/email/{account}/` (and `drafts/` underneath). PAI's workflow for sending:

- **Reply** — write a new yaml in today's `{date}/` directory with `direction: outbound`, `in_reply_to` set to the parent Message-ID. Driver detects the new file (no `message_id` field yet → unsent), calls Graph `/messages/{parent-id}/reply` (Graph preserves threading server-side), then back-fills `message_id` and `sent_at` into the same yaml. Updates `threads/` symlink.
- **New thread** — write a yaml under `drafts/{name}.yaml` with `to`, `subject`, `content`. Driver sends via Graph `/sendMail`, then moves the file to today's `{date}/` directory and back-fills `message_id`/`sent_at`.
- **Failure handling** — on permanent send failure, append a `kernel: send failed — {reason}` note to the yaml (as a top-level `kernel_notes` list) and emit a `send_failed` event. Cursor advances so we don't retry forever, mirroring the imessage outbound contract.

## meta.yaml (per account)

```yaml
account: arda@outlook.com
provider: microsoft-graph
client_id: <azure-app-id>
tenant_id: consumers
poll_interval_seconds: 60
created: 2026-04-25
```

## Open questions (defer past v1)

- Attachments: metadata-only for v1. v2 stores blobs under `{date}/_attachments/{message-id}/`.
- Folders/labels: ignored for v1 (everything from Inbox, sent items not mirrored).
- Search: out of scope — the agent uses `grep` over the yaml tree.
- Webhook push notifications (Graph subscriptions): require public HTTPS — defer; polling is fine for personal use.

## Why this shape

- **Per-account root** keeps multi-account support trivial and isolates auth/token state.
- **Date dir as primary** gives natural chronological browsing and aligns with how PAI already organizes message logs.
- **Thread index as symlinks** preserves the "filesystem is the data structure" principle without duplicating bytes.
- **YAML over markdown** because email has too many structured fields (headers, recipients, message-ids) to live cleanly in a `[HH:MM] sender:` flat-line format.
- **No backfill** keeps bootstrap fast and avoids importing years of mail PAI doesn't need.
