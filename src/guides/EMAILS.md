# EMAILS — Email Integration (Outlook + Gmail)

Design for PAI drivers that let the agent read and write email through personal Outlook accounts (Microsoft Graph) and personal Gmail accounts (Google Gmail API). The filesystem layout, YAML schema, and threading model are identical across providers — only the auth flow, change-detection mechanism, and send API differ.

## Scope

- **In:** read inbound mail, send outbound mail (including replies).
- **Out:** calendar, contacts, tasks, Drive/OneDrive, attachments-as-first-class, search, labels/categories. Add later as separate drivers.
- **No backfill.** Drivers start from "now" — bootstrap captures the current change-cursor (Graph delta token / Gmail `historyId`) and only ingests mail that arrives after that point. Existing mailbox contents are not imported.

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

- `{account}` is the full email address, e.g. `arda@outlook.com` or `arda@gmail.com`. Provider is recorded in `meta.yaml`, not the path — the directory shape is identical for both. Supports multiple accounts (across providers) cleanly.
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

attachments: []                               # [{name, size, content_type, provider_id}] — metadata only for v1
provider_thread_id:                           # Gmail threadId / Graph conversationId — opaque, used for replies
```

Outbound messages use the same schema with `direction: outbound`, `from` set to the account, and a `sent_at` field instead of `received_at`.

## Threading

Each message stores its `Message-ID`, `In-Reply-To`, and `References` headers. These are the source of truth and are identical across providers (RFC 5322). Provider-native thread IDs (Gmail `threadId`, Graph `conversationId`) are stored as `provider_thread_id` for send-side use but are **not** the canonical threading key — the slug derived from headers is.

**Thread slug** is derived once per thread and stable for its lifetime:

- Take the root Message-ID (the first entry in `References`, or the message's own `Message-ID` if it has no parent).
- Slug = `{normalized-subject}-{8-char-hash-of-root-message-id}`.
- Normalized subject = subject with `Re:`/`Fwd:`/`RE:`/etc. stripped, lowercased, whitespace collapsed, non-alphanumerics replaced with `-`.

**Two indexes** point back at the same canonical files in `{date}/`:

1. **`.prev` symlink** (optional, per-message) — `Re Hello World.prev` → `../2026-04-23/Hello World.yaml`. Gives one-hop walkback. Skipped when the parent can't be located locally (e.g. parent was sent before bootstrap).

2. **`threads/{thread-slug}/` dir** — symlinks named `{received-at}-{subject-slug}.yaml` so `ls` shows the whole thread in chronological order. Built incrementally as messages arrive; fully regenerable from yaml fields if it ever drifts.

Both indexes are derived. The `{date}/` files are authoritative.

## Drivers

Two independent driver pairs, one per provider. They share **only** the on-disk contract above (filesystem layout, per-message YAML, threading rules, event shapes). No shared base class, no provider abstraction layer — auth flows, cursor semantics, MIME handling, and the folder-vs-label model diverge enough that a unifying interface would be more drag than the duplication is worth.

The account → driver mapping is decided at startup by reading each `meta.yaml`'s `provider` field and launching the matching pair.

### Shared on-disk contract

What both drivers must produce regardless of provider:

- Per-message YAML written to `{date}/{subject-slug}.yaml` with the schema above.
- `threads/{thread-slug}/` symlink updated; best-effort `.prev` symlink.
- A `new_email` event into `live/events/` with `{source: email, account, provider, thread_slug, subject, from, path}`.
- Bootstrap captures the current cursor and writes nothing.
- A first post-bootstrap sync that returns a backlog emits a single `email_backlog` event, not N events (mirroring imessage's coalesced backlog).
- Permanent send failures append a `kernel_notes` entry to the yaml and emit `send_failed`; the cursor advances so we don't retry forever.

### Outlook driver pair (`src/drivers/email/outlook/`)

`inbound.py`:
- Auth: MS Graph OAuth via device-code flow. Refresh token cached at `live/tmp/drivers/email/{account}/token.json`. Public client (no secret) — works with personal outlook.com accounts (tenant = `consumers`) registered with `Mail.ReadWrite`, `Mail.Send`, `offline_access`, `User.Read`.
- Change detection: Graph **delta query** (`/me/mailFolders/Inbox/messages/delta`). Cursor stored at `live/tmp/drivers/email/{account}/delta-token`. Subsequent polls pull only changes since the last token.
- Body fetch returns HTML or text; convert HTML to text on ingest.

`outbound.py`:
- **Reply:** new yaml in today's `{date}/` with `direction: outbound`, `in_reply_to` set. Driver calls `POST /messages/{parent-id}/reply` — Graph preserves threading server-side from the parent id alone.
- **New thread:** yaml under `drafts/{name}.yaml`. Driver calls `POST /sendMail` with the body, then moves the file into `{date}/` and back-fills `message_id`/`sent_at`/`provider_thread_id` (= `conversationId`).

### Gmail driver pair (`src/drivers/email/gmail/`)

`inbound.py`:
- Auth: Google OAuth 2.0 via installed-app loopback flow (`http://127.0.0.1:<port>/`). Refresh token cached at `live/tmp/drivers/email/{account}/token.json`. Scopes: `https://www.googleapis.com/auth/gmail.modify` and `https://www.googleapis.com/auth/gmail.send`. Client ID/secret come from a Google Cloud "Desktop app" credential (the secret is non-confidential for installed apps).
- Change detection: Gmail **history API** (`users.history.list?startHistoryId={cursor}`). Bootstrap uses `users.getProfile` to capture the current `historyId`. Cursor stored at `live/tmp/drivers/email/{account}/history-id`.
- `historyId` expires after ~7 days of inactivity. On `404 historyId not found`, fetch a fresh `getProfile` cursor and emit a single `email_cursor_reset` event — never backfill.
- Filter: only messages whose `labelIds` include `INBOX` and exclude `DRAFT`/`SENT`. Spam/trash ignored.
- Body comes as base64url-encoded MIME parts; walk the parts tree and convert HTML to text.

`outbound.py`:
- **Reply:** new yaml in today's `{date}/` with `direction: outbound`, `in_reply_to` set. Driver builds an RFC 5322 MIME message — copies `References` from the parent yaml and appends the parent's `Message-ID`, sets `In-Reply-To`, sets `Subject` to `Re: {parent-subject}` (Gmail requires this for the conversation grouping to stick), base64url-encodes it, then `users.messages.send` with `threadId` set to the parent's `provider_thread_id`.
- **New thread:** yaml under `drafts/{name}.yaml`. Build MIME, `users.messages.send` with no `threadId`, then move the file into `{date}/` and back-fill `message_id`/`sent_at`/`provider_thread_id` (= Gmail's `threadId`).

## meta.yaml (per account)

Outlook:

```yaml
account: arda@outlook.com
provider: microsoft-graph
client_id: <azure-app-id>
tenant_id: consumers
poll_interval_seconds: 60
created: 2026-04-25
```

Gmail:

```yaml
account: arda@gmail.com
provider: gmail
client_id: <google-oauth-client-id>
client_secret: <google-oauth-client-secret>   # non-confidential for installed apps
poll_interval_seconds: 60
created: 2026-04-25
```

## Open questions (defer past v1)

- Attachments: metadata-only for v1. v2 stores blobs under `{date}/_attachments/{message-id}/`.
- Folders/labels: ignored for v1 (everything from Inbox, sent items not mirrored).
- Search: out of scope — the agent uses `grep` over the yaml tree.
- Webhook push notifications (Graph subscriptions / Gmail Pub/Sub watch): require public HTTPS — defer; polling is fine for personal use.

## Why this shape

- **Per-account root** keeps multi-account support trivial and isolates auth/token state.
- **Date dir as primary** gives natural chronological browsing and aligns with how PAI already organizes message logs.
- **Thread index as symlinks** preserves the "filesystem is the data structure" principle without duplicating bytes.
- **YAML over markdown** because email has too many structured fields (headers, recipients, message-ids) to live cleanly in a `[HH:MM] sender:` flat-line format.
- **No backfill** keeps bootstrap fast and avoids importing years of mail PAI doesn't need.
