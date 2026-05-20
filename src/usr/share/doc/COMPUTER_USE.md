# COMPUTER_USE

> **Status: split spec.** Part 1 describes the `browse` capability as it
> exists in the codebase today. Part 2 is the original design brainstorm
> kept as a roadmap — none of it ships yet. Path authority for anything
> on disk is `FILESYSTEM_v3.md`.

## The bet

Instead of writing N service drivers (Gmail, Calendar, Linear, Notion,
…), give PAI **one** general capability: a browser it can drive.
Anything a human can do through a webpage, PAI can do too. Web search,
form submission, dashboards, SaaS tools — all reachable through the
same primitive.

---

# Part 1 — Shipped today

## Shape

`browse` ships as **two pairegistry packages**, installed into the
running FHS the same way every other userspace bundle is:

- **`bin/browse`** (`~/Projects/pairegistry/bin/browse/`, installed to
  `/usr/lib/bins/browse/` and shimmed on `$PATH` as `browse`). Thin CDP
  verbs against the owner's real Chrome — one CDP WebSocket per
  invocation, one action, exit.
- **`subagent/browse`** (`~/Projects/pairegistry/subagents/browse/`,
  installed to `/usr/lib/subagents/browse/`). A subagent bundle whose
  `prompt.md` teaches a model to drive the `browse` verbs across
  multiple bash turns.

A parent PAI invokes it the standard subagent way:

```
subagent spawn --bundle browse --message "<task>"
```

The kernel forks a `/proc/<slug>/` for the subagent, kicks off its
prompt, and the subagent's own bash shell is the agent loop — there is
no nested LLM. When done, the subagent writes
`/proc/<slug>/result.md` and calls `subagent kill --slug $PAI_SLUG`,
which the parent reads.

## Execution model

**One mode: CDP attach to the owner's real Chrome.** No bundled
Chromium, no separate profile. Chrome launches lazily on the owner's
real `~/Library/Application Support/Google/Chrome` profile with
`--remote-debugging-port=9222`. WAFs see a returning logged-in human,
not a bot.

## Verbs

The `browse` bin exposes a flat verb set; each call is a single CDP
command:

```
browse goto <url>
browse text [--max-chars N]
browse dom                                  # numbered interactive elements
browse click <idx>
browse type <idx> "<text>" [--submit]
browse press <key>
browse scroll [down|up|N]
browse screenshot [path]
browse url
browse title
browse wait <selector|text> [--timeout S]
browse tabs
browse claim <tab_id>
browse close
```

Indices come from the most recent `browse dom` and are invalidated by
the next nav/click.

## Tabs and handoff between spawns

Each browse subagent owns one tab. When the subagent exits, the
kernel marks its tab as orphaned in
`/sys/drivers/browse/tabs/<slug>.yaml`
(see `boot/processes.py`). On the next browse spawn, `bin/subagent.py`
prefixes the kickoff message with an `AVAILABLE TABS` block listing
claimable orphans. The new subagent can `browse claim <tab_id>` to
inherit context (cookies, scroll position, prior page) or ignore the
list and open a fresh tab.

This is the only cross-spawn state today. Tab metadata lives under
`/sys/drivers/browse/`; the per-spawn artifact is the subagent's
`/proc/<slug>/result.md`.

## Auth model

PAI does not hold passwords. It drives the owner's real Chrome
profile, so whatever the owner is signed into is what browse can act
on. No OAuth dance, no cookie import, no separate sign-in step.

## Result contract

One file per spawn: `/proc/<slug>/result.md`. Markdown, ≤500 lines,
includes the final URL, the answer, and any key verbatim quotes. On a
hard block (login wall, captcha, dark site) the subagent still writes
`result.md` describing the failure and exits — the parent reads
closure either way and does not retry.

---

# Part 2 — Open / deferred

Everything below is design rationale and roadmap, **not** shipping
behavior. Where the brainstorm conflicted with what shipped, Part 1
wins.

## Page → text representation

The shipped path is `browse dom` (numbered, accessibility-leaning
snapshot of interactive elements) plus `browse text` for prose. The
original sketch had a richer, page-structured text surface:

```
URL: https://foo.com
TITLE: Foo — Sign in

CHECKBOXES:
  [X] Remember me                 #remember
INPUTS:
  email    (empty)                #email
BUTTONS:
  [ Log in ]                      #submit
LINKS:
  Forgot password?                /reset
```

Open whether to grow `browse dom` into this shape or keep it minimal.

## Approach options for the action surface

- **A. DOM + accessibility tree.** What the shipped `browse dom` is
  closest to. Deterministic, fast, no model in the loop.
- **B. VLM on screenshots.** Anthropic computer-use style. Slow,
  expensive, works on anything visible.
- **C. Hybrid.** Accessibility default, VLM fallback when the a11y
  tree is empty or an action fails to change the page.
- **D. Record-and-replay per site.** First run records a script;
  subsequent visits replay. Complement to A/B/C, not a replacement.

## Mid-task human handoff

Generalized "pause and hand the browser to the human mid-task" flow
("I need you to log in to foo.com — taking control now", human logs
in, PAI resumes) is **not** shipped. Today auth is pre-seeded by
virtue of using the owner's profile.

## Per-domain action log

The brainstorm called for an append-only event log at
`<pai-home>/communication/browser/<domain>/YYYY-MM-DD.md` in the same
format as messages, so browsing becomes just another conversation. Not
implemented. The only artifact today is per-spawn `result.md`.

## Sandboxing

No per-domain allowlists, no POST-confirmation prompts, no jailing. A
general browser driven by an LLM is a large blast radius and this slot
is open.

## Long-running browser daemon

Today each spawn either reuses an existing CDP Chrome (port 9222
alive) or launches one. There is no supervisor managing tabs, sessions,
or concurrent spawns across the fleet. Tab handoff via the orphan
mechanism is the minimal version of this; a real daemon is deferred.

## Failure recovery

If an action's expected outcome doesn't materialize (clicked submit,
still on same URL, no new text), there is no kernel-level retry/replan
loop. The subagent's own bash turns are the only retry surface. WAF
hard-blocks exit with `result.md` and no retry, by design.

## Next concrete step

Pick 5 representative sites (static blog, Gmail, a SaaS dashboard, a
government form, a search engine), run the current `browse dom` /
`browse text` against each, and eyeball where the a11y-leaning surface
breaks. If 3/5 are usable as-is, the shipped approach extends
naturally. If most are garbage, prototype the hybrid (C).
