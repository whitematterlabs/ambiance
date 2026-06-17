# Getting to Know You — OOBE owner-profiling pass

**Date:** 2026-06-17
**Status:** Design approved, pending spec review

## Problem

The first thing PAI does after OOBE setup — before the owner types anything —
should be to get to know who it serves. Today a fresh PAI knows nothing about
the owner and has to learn everything through conversation. We want it to
bootstrap an owner profile from ambient data (mail, iMessage, contacts,
calendar, WhatsApp), persist it as a single canonical reference, inject that
reference into every PAI's system prompt, and surface its findings to the owner
for correction.

## Goals

- One-time pass on first wake after OOBE, before any owner message.
- Build a structured, plain-text profile of the **owner** (not a self-persona).
- Single canonical file, loaded wholesale into the system prompt of **every**
  PAI in the fleet.
- Surface findings in owner chat so the owner can verify / disagree; corrections
  edit the canonical file directly.
- Living document — the OOBE pass builds v1; PAI keeps refining over time.

## Non-goals

- No confidence scoring on claims. The verify loop is the safety net; scoring
  adds noise without value here.
- No separate memory-subset or per-home symlink. Whole-file prompt injection
  supersedes both.
- No new driver. The profiling pass reads sources directly; it does not depend
  on driver event streams.

## Decisions (from brainstorming)

| Decision | Choice |
|---|---|
| Subject of the profile | The **owner** (facts), not a PAI self-conception. |
| Read window | **Last month.** |
| Mail source | `bin/mailsearch` (bounded query). |
| iMessage source | **Direct bounded `chat.db` query** — the driver does NOT backfill (sets `last_rowid` to `MAX(ROWID)` on init, tracks forward only). `chat.db` timestamps are ns since 2001-01-01 UTC; filter by date directly. |
| Other sources | `contacts`, `calendar`, `whatsapp` (read paths via their drivers/skills). |
| Consent | **Announce, then proceed.** PAI states what it will read; owner can stop it. No blocking pre-gate. |
| Confidence field | **Dropped.** |
| Storage / consumption | One canonical `owner-profile.md`; **entire contents injected into the system prompt of every PAI.** |
| Who runs it | `pai_default` — reading owner surfaces is its job, not root's. |

## Architecture

### 1. Trigger
A single boolean gate in `/etc/config.yaml` (source of truth, greppable):
`onboarding_pending: true`.

- `paisetup` sets it `true` at the end of OOBE.
- On wake, `pai_default` checks it. If `true`, run the pass, then set it
  `false`. If `false`/absent, the pass is a no-op.

This is the run-once gate expressed as a bool. Side benefit: if the pass is
interrupted partway (crash, owner says stop, partial read), the flag stays
`true` and the pass simply re-runs on the next wake — idempotent retry, no
separate "in progress" state needed.

### 2. The profiling pass (pairegistry skill/bin, run by `pai_default`)
1. **Announce** in owner chat: roughly *"I'm going to skim your recent mail,
   messages, contacts, and calendar from the last month to get to know you —
   say stop anytime."*
2. **Bounded read** (last month):
   - Mail: `bin/mailsearch`.
   - iMessage: direct bounded `chat.db` query (last month by date).
   - Contacts, calendar, WhatsApp: via existing read paths.
   - Sample intelligently (top contacts by frequency, recent threads) rather
     than ingesting everything — keeps tokens and noise down.
3. **Synthesize** a structured profile. Plain claims, no confidence field;
   note where something came from only when it isn't obvious.
4. **Write** the canonical file (see §3).
5. **Surface** a skimmable digest in owner chat, linking the file. Non-blocking:
   PAI starts using v1 immediately.

### 3. Canonical file
- Path: `/var/lib/owner/profile.md` (proposed; confirm at review).
- Sections: identity (name, timezone, comm style), work, key people / social
  graph, recurring patterns, preferences. Facts about *people around the owner*
  noted as more sensitive.
- Plain markdown, greppable/editable — corrections are just edits to this file.

### 4. Prompt injection (kernel — this repo)
- `src/boot/bootstrap.py`: add an `_owner_profile_block(home)` that reads the
  canonical file wholesale and returns it as a system-prompt layer.
- Slot it in `build_system_prompt` right after `_memory_index_block(home)`
  (bootstrap.py:742), so every PAI — fleet and (optionally) subagents — sees the
  full profile. Missing/empty file → empty block (no error), matching the
  location-hint pattern.

### 5. Verify loop
- Owner skims the digest in chat, strikes/edits what's wrong.
- PAI edits the canonical file directly. Because the whole file is in-prompt for
  every PAI, the next assembly reflects corrections fleet-wide. Wrong claims are
  removed/rewritten in place — no separate negative-fact store needed.

### 6. Empty case
Fresh Mac / empty inbox → no hallucinated bio. Degrade to *"I don't have much to
go on yet — tell me about yourself"* and build the profile from conversation.

## Code locations

- **Kernel (this repo):** `src/boot/bootstrap.py` — new `_owner_profile_block`
  layer + wiring in `build_system_prompt`. Keep `src/bin` ↔ pairegistry in sync
  if any dual-homed bin changes.
- **Profiling pass (pairegistry):** a skill or `bin/` that `pai_default` invokes
  on first post-OOBE wake. Lives in `~/Projects/pairegistry/`, not `src/`.
- **OOBE trigger:** `src/sbin/paisetup/app.py` is the existing OOBE surface;
  determine there how the first-wake pass is kicked off and where the
  run-once sentinel lives.

## Open questions for review

1. Confirm canonical path `/var/lib/owner/profile.md`.
2. Should the profile inject into **subagent** prompts too, or fleet PAIs only?
3. Exact trigger mechanism: how `paisetup` signals "OOBE done → run the pass,"
   and where the run-once sentinel lives.
