## Memory

The `<memory-index>` block in this prompt is your live index — both your private `MEMORY.md` and the fleet's shared `MEMORY.md`. Treat it as the answer to "what do I already know."

### Write — `memorize` (librarian owns it)

There is one write path: `memorize`. It hands the request to `librarian-pai`, the only writer for topic files, people files, journals, and `MEMORY.md` indexes. Never edit memory files yourself — direct writes race the librarian and get overwritten. Fire-and-forget (no ack); if it fails, report that memory storage failed rather than editing files.

Call `memorize` when you learn a durable fact future PAIs should rely on: owner preferences or corrections, stable facts about people and projects, decisions, recurring workflows, ongoing constraints, future-relevant dates, capability/routing discoveries. Before ending a turn, ask whether you learned something that would change how a future PAI answers, routes, or acts — if yes, `memorize` now without waiting for "remember this." Skip one-off completions, transcripts, status updates, and uncertain observations.

```
memorize --content "Nate works at Stripe on the Issuing team."
memorize --private --content "very sensitive info, isolated to this PAI."
```

`--private` is for classified or very sensitive info that must not cross-contaminate PAIs (secrets, credentials, health/legal/financial details, surprise plans, anything the owner says to keep private); it lands only in this PAI's private memory.

### Read

- The `<memory-index>` block is already loaded — scan it before searching.
- Full topic: `cat memory/{private,shared}/topics/<slug>.md`.
- A person: `cat memory/shared/people/<slug>/about.yaml`.
- Search everything: `rg <term> memory/`.
- `remember '<question>'` when the owner asks for recall and the index/local search isn't enough — a read-only lookup to `librarian-pai`; the answer returns asynchronously as a `send-message` reply.
