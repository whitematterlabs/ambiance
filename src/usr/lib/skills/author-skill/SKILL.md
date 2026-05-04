---
name: author-skill
description: Howto for writing a new skill — frontmatter, content shape, when to make one vs add to an existing skill. The convention this very file follows.
---

# Authoring a skill

A skill is a focused, self-contained capability or piece of
knowledge a PAI can pull in on demand. Live at
`memory/skills/<name>/`.

Skills come in two flavors:
- **Action skills** (e.g. `reload-config`, `restart-driver`) — a
  procedure to run when a specific situation lands.
- **Knowledge skills** (e.g. `understand-kernel`, `author-driver`)
  — a primer the PAI reads to orient itself before acting.

Both use the same shape.

## File layout

```
memory/skills/<name>/
└── SKILL.md         # required; frontmatter + body
```

A skill *may* ship additional files (templates, scripts, examples).
Reference them from SKILL.md by relative path. The PAI reads
SKILL.md first.

## Frontmatter (required)

```markdown
---
name: <kebab-case-name>      # must equal the directory name
description: <one line>      # used to decide relevance — be specific
---
```

The description is what the PAI scans when deciding whether to pull
the skill in. Lead with the trigger ("Use when …", "Read first to
understand …"); avoid vague nouns like "helpful" or "stuff."

## Body

Keep skills **focused**. A skill should answer one question or
walk one procedure. Long generic explainers belong in
`memory/doc/` — skills point to them.

Recommended sections:
- **When to use** — the trigger that should pull this skill in.
- **Procedure** (action) or **Concepts** (knowledge) — the meat.
- **When NOT to use / Boundaries** — what to escalate or skip.
- **Verification** (action) — how to confirm success.
- **Read these next** — links to docs and adjacent skills.

## Writing style

- Terse. The PAI is reading at runtime under context pressure.
- Concrete paths and command snippets over prose.
- Cross-link by skill name (e.g. ``skill `understand-event-routing` ``)
  and to docs by absolute path under `memory/doc/`.
- Don't duplicate content from `memory/doc/` — point to it.

## When to make a new skill vs extend an existing one

**New skill** when:
- A new triggering situation needs its own playbook.
- A new system area needs a knowledge primer no existing skill covers.
- An existing skill would balloon past ~80 lines if you added it.

**Extend** when:
- The same procedure with a new edge case.
- A clarifying note that fits within the existing structure.

## Don't

- Don't repeat what `understand-filesystem` or `understand-kernel`
  already say. Link to them.
- Don't bake operator-only decisions into a skill (provider/model
  choices, adding/removing a PAI). Surface to operator.
- Don't write skills that duplicate driver `events.yaml`. The
  manifest is the source of truth for kinds.

## Read these next

- `memory/skills/reload-config/SKILL.md` — reference action skill.
- `memory/skills/understand-kernel/SKILL.md` — reference knowledge skill.
