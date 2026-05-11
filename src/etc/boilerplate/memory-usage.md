## Memory layout

You have two tiers of memory, both stitched into your home as `memory/`:

- **`memory/private/`** — your sacred per-instance state. Only you write here.
  - `private/journal/YYYY-MM-DD.md` — append-only running log.
  - `private/topics/<slug>.md` — durable notes you've decided are worth keeping.
  - `private/MEMORY.md` — a compact index of your private topics (cap ~150 lines).
- **`memory/shared/`** — fleet-wide state visible to every PAI.
  - `shared/journal/YYYY-MM-DD.md` — append-only running log shared by the fleet.
  - `shared/topics/<slug>.md` — durable cross-PAI knowledge.
  - `shared/people/<slug>/about.yaml` — contact records.
  - `shared/MEMORY.md` — compact fleet-wide index.

Both `MEMORY.md` files are loaded on boot, so keep them tight.

## Write rules — read carefully

You may **write freely** to:
- `memory/private/` — anything in your own private dir.
- `memory/shared/journal/<today>.md` — append-only. Multiple PAIs append here; never edit or rewrite past lines.

You **must NOT write** to:
- `memory/shared/topics/`
- `memory/shared/people/`
- `memory/shared/MEMORY.md`

Those are owned by the **librarian PAI**, which runs nightly and consolidates journals into durable topic/people files. If you edit them directly you'll race the librarian and your edits will be overwritten or fight other PAIs. Instead: **append the fact to today's shared journal** and let the librarian promote it.

## When to write what

- **Heard something worth remembering across PAIs?** Append a line to `memory/shared/journal/<today>.md`. The librarian decides if it's durable.
- **Personal preference / lesson / recurring context for just you?** Edit `memory/private/topics/<slug>.md` and update `memory/private/MEMORY.md` if it's a new topic.
- **Routine work log / what you did this turn?** Append to `memory/private/journal/<today>.md`.
- **Owner told you a fact about a person?** One line to `memory/shared/journal/<today>.md` (e.g. "kaia: switched jobs to Anthropic 2026-04"). The librarian rolls it into `shared/people/kaia/about.yaml` overnight.

## Reading

Read anything you need, on demand. Common patterns:
- `cat memory/shared/MEMORY.md` and `cat memory/private/MEMORY.md` — already in your boot context, but re-read if the turn is long.
- `rg <term> memory/shared/` — find what the fleet knows.
- `cat memory/shared/people/<slug>/about.yaml` — before talking to or about someone.
- `ls memory/shared/journal/ | tail -7` — last week of fleet activity.

## Naming

- Journal files: `YYYY-MM-DD.md` (one per day, append-only).
- Topic / people slugs: lowercase, hyphenated, no spaces (`kaia`, `q3-launch`, `whitematter-ops`).
- Don't create new top-level dirs under `memory/shared/` — the librarian only knows about `journal/`, `topics/`, `people/`, `MEMORY.md`.
