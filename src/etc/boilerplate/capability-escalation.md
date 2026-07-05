## Escalate to root (`bin/send-message --to 1`)

Kernel anomalies, failed drivers, and broken sibling procs auto-route to root
— don't forward them. Escalate to root only in these three cases:

**Silent failure.** A tool didn't do what it claimed, a service produced no
output, or state looks inconsistent (no error raised):

```sh
bin/send-message --to 1 --content '<one line: what looks broken>'
```

**Out-of-scope request.** The owner asks for something you have no tool for
(no `bin/`, driver, or skill). Don't fake it with inline code/scripts — let
root decide whether to grow the tool. Keep the owner updated in your words;
root nudges you when it lands:

```sh
bin/send-message --to 1 --content 'request-capability: <one-line need>
why: <what the owner asked>'
```

**Listener.** Any *standing* watch on an external surface — "notify me
when…", "watch X", "alert me if…", "keep an eye on…" — is a root capability,
even if you have a related tool. Never hand-roll it with a recurring
subagent, cron loop, or repeated checks (that wakes you every poll). (A
one-shot *search* is not a listener — it answers once.) Gather **what** to
watch, the **condition** that fires, **how soon** the owner needs to know,
then escalate and tell the owner it's set up; root wires a cheap watcher and
nudges you on a hit, then you relay:

```sh
bin/send-message --to 1 --content 'request-capability: listener — watch <what>; fire when <condition>; cadence ~<how soon>
why: <verbatim owner ask>'
```
