# COMPUTER_USE

## Status — what shipped, what's still aspirational

This file started as a design exploration (sections below). Some of it is now
real and lives in the `browse` subagent (`/usr/lib/subagents/browse/`); the
rest is still open. Quick map:

**Shipped** (use the `browse` subagent — see its `prompt.md`):
- [browser-use](https://github.com/browser-use/browser-use) as the agent
  loop, with Playwright purely as a CDP client transport. Accessibility-
  tree-first with VLM fallback baked into the library.
- **One execution mode: CDP attach to the owner's real Chrome.** No
  bundled Chromium, no separate profile. `entry.py` takes over the
  owner's running Chrome (quits it first, SQLite-corruption guard) and
  relaunches it against `~/Library/Application Support/Google/Chrome`
  with `--remote-debugging-port=9222`. WAFs see a returning logged-in
  user, not a bot.
- One result file per spawn at `/proc/$PAI_SLUG/result.md`. Exit code
  `2` + `WAF_BLOCKED: <host>` marker when even the real Chrome gets
  walled — parents should not retry.

**Auth model in practice (replaces the "hands the browser to the human"
sketch below):** PAI doesn't hold passwords. It uses the owner's real
Chrome profile, so whatever the owner is signed into is what browse can
act on. No separate sign-in step.

**Still open:**
- Generalized "pause and hand the browser to the human mid-task" flow. Today
  it's pre-seeded sign-in, not interactive handoff.
- Per-domain action log under `home/communication/browser/{domain}/`. Today
  the only artifact is `result.md`.
- Sandboxing / per-domain allowlists / POST-confirmation prompts. None.
- Long-running browser daemon model. Today each spawn either reuses an
  existing CDP Chrome (port 9222 alive) or launches one; there's no
  supervisor managing tabs across spawns.

The rest of this doc is the original brainstorm — kept as design rationale.

---

## The bet

Instead of writing N custom integrations (Gmail driver, Calendar driver, Linear driver, Notion driver, ...), give PAI **one** general capability: a browser it can drive. Anything a human can do through a webpage, PAI can do too.

Why this is the right shape:
- Auth is delegated to the human at first contact, then the session cookie carries forward. No OAuth dance per service.
- Web search, form submission, dashboards, SaaS tools, government portals — all reachable through the same primitive.
- Aligns with PAI's "filesystem + plain text" ethos: pages collapse to text, actions are append-only events.

## The hard part

A browser is a graphical thing. PAI thinks in text. We need a layer that turns "the current page" into a **textual traversal surface** — an enumerated, labeled, navigable description of what's actionable right now.

Target representation (sketch):

```
URL: https://foo.com
TITLE: Foo — Sign in

CHECKBOXES:
  [X] Remember me                 #remember
  [ ] Subscribe to newsletter     #newsletter

INPUTS:
  email    (empty)                #email
  password (empty)                #password

BUTTONS:
  [ Log in ]                      #submit
  [ Sign up ]                     /signup
  [ Continue with Google ]        oauth:google

LINKS:
  Forgot password?                /reset
```

PAI then issues actions like `click #submit`, `fill #email = "..."`, `goto /signup`. The layer round-trips: action → real browser event → new page → re-rendered text surface.

## Wishful-thinking premise

> A browser ultimately turns user actions into HTTP requests + JS execution. So there must be a way to enumerate the action surface of a page as text.

Mostly true, with caveats:

- **Static actions** (anchor hrefs, form actions, declared event handlers) are enumerable from the DOM directly. Easy.
- **Dynamic actions** (JS-attached `onclick`, React/Vue synthetic handlers, delegated listeners on `document`) are *not* enumerable by reading HTML. The handler exists only as a function reference inside the JS runtime. You cannot statically list "all possible POSTs this page can make."
- **Conditional UI** (modals that mount on click, infinite scroll, tooltips) means the action surface at time T is a subset of the surface at time T+1. Enumeration is a moving target.

So pure reverse-engineering of the HTTP surface doesn't work. We need a hybrid.

## Approach options (still undecided)

### A. DOM scrape + accessibility tree
Use a real headless browser (Chromium via CDP). After every navigation, walk the **accessibility tree** (not raw DOM) — that's what screen readers use, and it already labels things by role (button, checkbox, link, textbox) with their accessible names. Stable, semantic, ignores presentational divs.

- Pros: deterministic, fast, no model in the loop, uses browser-native semantics.
- Cons: misses sites with bad a11y; dynamic widgets (custom dropdowns built from `<div>`s) may show up as junk.

### B. Vision-language model on screenshots
Screenshot the page, ask a VLM "list all interactive elements with bounding boxes and labels." Anthropic's computer-use API is literally this.

- Pros: works on anything a human can see; doesn't care about DOM hygiene.
- Cons: slow, expensive per step, non-deterministic, hard to test.

### C. Hybrid (likely winner)
Accessibility tree as the default cheap path. Fall back to VLM when the a11y tree is empty/garbage or when an action fails ("I clicked #submit and nothing changed → take a screenshot and re-plan").

### D. Record-and-replay per site
First time PAI uses a site, a human (or VLM) walks through the flow and PAI saves a script. Subsequent visits replay the script. Re-record on breakage.

- Pros: fast and reliable in steady state.
- Cons: doesn't generalize; brittle to redesigns. Probably a complement to A/B/C, not a replacement.

## Auth model (original sketch — see Status block at top for what shipped)

- PAI never holds raw passwords. When a site needs login, PAI **pauses and hands the browser window to the human** ("I need you to log in to foo.com — taking control of the browser now"). User logs in, PAI resumes with the session cookie.
- Per-domain cookie jars stored under `home/workspace/browser/cookies/{domain}/`.
- Re-auth is a normal pause-and-handoff event; not an error.

> What actually shipped is simpler: browse takes over the owner's real
> Chrome (real Default profile) over CDP. Whatever the owner is signed
> into is what browse can use. No separate profile, no cookie import,
> no interactive mid-task handoff yet.

## Open questions

1. **Browser runtime.** Playwright vs CDP-direct vs Chrome via DevTools Protocol over a long-lived process? PAI's bash tool is a literal TTY (per memory), so a long-running browser daemon controlled via a CLI fits naturally.
2. **State boundary.** Does the page-text representation live in PAI's context window every step, or in a file under `home/` that PAI reads on demand? Probably the latter, with a short summary in context.
3. **Action log.** Every click/fill/goto should append to `home/communication/browser/{domain}/YYYY-MM-DD.md` in the same format as messages — so browsing is just another conversation.
4. **Concurrency.** One browser, many tabs? Many browsers? When PAI is doing background work and the user grabs the browser to log in, what happens?
5. **Failure recovery.** If an action's expected outcome doesn't materialize (clicked submit, still on same URL, no new text), what's the retry/replan loop?
6. **Sandboxing.** A general-purpose browser controlled by an LLM is a large blast radius. Per-domain allowlists? Confirmation prompts before POSTs to unfamiliar origins?

## Next concrete step

Prototype the page→text layer in isolation. Pick 5 representative sites (a static blog, Gmail, a SaaS dashboard, a government form, a search engine), run Playwright + accessibility-tree extraction on each, eyeball the output. If 3/5 are usable as-is, approach A is viable and we build from there. If most are garbage, jump to the hybrid.
