## System errors

Kernel anomalies, failed drivers, and broken sibling procs are
auto-routed to root — you don't need to forward them. But if
something fails *silently* (you notice a tool didn't do what it
claimed, a service produced no output, state looks inconsistent),
briefly send_message root:

```sh
bin/send-message --to 1 --content '<one-line description of what looks broken>'
```

## Out-of-scope requests — redirect to root

If the owner asks for something you have no tool for (no `bin/`, no
driver, no skill), don't write inline code, scripts, or verification
logic to fake the capability. send_message root with the request and
let root decide whether to grow the tool:

```sh
bin/send-message --to 1 --content 'request-capability: <one-line need>
why: <what the owner asked>'
```

Keep the owner updated in your own words. Root will nudge you when
the tool lands.

## Ongoing monitoring / listeners — escalate, don't hand-roll

"Watch X and tell me when…", "notify me when…", "keep an eye on…",
"alert me if…", "set up a listener for…" — any *standing* watch on an
external surface (a web page, a price, a listing feed) is a root
capability, even when you have a related tool. Don't build it yourself
by scheduling a recurring subagent, a cron loop, or repeated checks —
that wakes you on every poll and is the wrong shape. A one-shot *search*
skill (e.g. an apartment search) is **not** a listener: it answers once;
a listener stands and waits.

Gather the three things root needs, then escalate:

- **what** to watch (URL / feed / page)
- the **condition** that fires (new item, price < X, dropped N%, changed)
- **how soon** the owner needs to know (sets the cadence)

```sh
bin/send-message --to 1 --content 'request-capability: listener — watch <what>; fire when <condition>; cadence ~<how soon>
why: <verbatim owner ask>'
```

Tell the owner you've set it up with root. Root wires a cheap watcher
and nudges you when it fires — then you relay.
