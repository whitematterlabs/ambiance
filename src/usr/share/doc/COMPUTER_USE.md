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
bin/subagent spawn --slug browse --package browse --prompt "<task>"
```

The kernel forks a `/proc/<slug>/` for the subagent, kicks off its
prompt, and the subagent's own bash shell is the agent loop — there is
no nested LLM. When done, the subagent writes its report under the
parent workspace and calls `bin/subagent done --result result.md`; that
emits a completion pointer to the parent and resolves the subagent proc.

## Execution model

**One mode: CDP against PAI's own dedicated Chrome.** No bundled Chromium.
Chrome launches lazily against a PAI-owned profile under `PAI_ROOT`
(`var/chrome/profile`), seeded once from the owner's real
`~/Library/Application Support/Google/Chrome` profile so logged-in sessions
carry over — WAFs still see a returning logged-in human, not a bot. It runs
on a PAI-dedicated debug port (`--remote-debugging-port=9333`, deliberately
**not** the conventional 9222) so PAI never attaches to the owner's everyday
Chrome; `_ensure_chrome` verifies the process answering the port is PAI's own
profile before driving it, and refuses otherwise. Chrome 136+ also blocks
remote debugging when `--user-data-dir` resolves to the default profile path,
which the dedicated profile sidesteps.

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
browse close
```

Indices come from the most recent `browse dom` and are invalidated by
the next nav/click.

## Tabs between spawns

Each browse subagent owns one tab. When the subagent exits, the
kernel marks its tab as orphaned in
`/sys/drivers/browse/tabs/<slug>.yaml`
(see `boot/processes.py`). On the next normal `browse` use, the binary
closes orphaned tabs from previous browse subagents, removes their stale
metadata/snapshots, and opens a fresh tab for the new subagent.

Tab metadata lives under
`/sys/drivers/browse/`; the per-spawn handoff is the durable result file
referenced by the final `subagent:response`.

## Auth model

PAI does not hold passwords. It drives the owner's real Chrome
profile, so whatever the owner is signed into is what browse can act
on. No OAuth dance, no cookie import, no separate sign-in step.

## Result handoff

One final `subagent:response` per spawn, sent with
`bin/subagent done --result result.md` after writing
`$PAI_RESULT_DIR/result.md`. The result markdown
includes the final URL, the answer, and any key verbatim quotes. On a
hard block (login wall, captcha, dark site) the subagent still writes a
failure report and completes — the parent gets closure either way and
does not retry.

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
implemented. The result handoff today is the durable report file pointed
to by the final `subagent:response`.

## Sandboxing

No per-domain allowlists, no POST-confirmation prompts, no jailing. A
general browser driven by an LLM is a large blast radius and this slot
is open.

## Long-running browser daemon

Today each spawn either reuses PAI's existing CDP Chrome (the dedicated
port 9333 alive *and* owned by PAI's profile) or launches one. There is no supervisor managing tabs, sessions,
or concurrent spawns across the fleet. Tab handoff via the orphan
mechanism is the minimal version of this; a real daemon is deferred.

## Failure recovery

If an action's expected outcome doesn't materialize (clicked submit,
still on same URL, no new text), there is no kernel-level retry/replan
loop. The subagent's own bash turns are the only retry surface. WAF
hard-blocks finish with a final failure response and no retry, by design.

## Next concrete step

Pick 5 representative sites (static blog, Gmail, a SaaS dashboard, a
government form, a search engine), run the current `browse dom` /
`browse text` against each, and eyeball where the a11y-leaning surface
breaks. If 3/5 are usable as-is, the shipped approach extends
naturally. If most are garbage, prototype the hybrid (C).
