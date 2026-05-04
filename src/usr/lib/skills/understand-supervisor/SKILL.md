---
name: understand-supervisor
description: How the kernel supervises PAI processes — start, nudge, restart policy, failure capture. Read before diagnosing a /proc/<pai>/ that's stuck or thrashing.
---

# Process supervision

KernelPAI supervises **the PAI process**, not what's inside it.
Each long-running PAI gets a supervisor coroutine in the kernel
that owns its lifecycle.

## What the supervisor does

For every active PAI in `/proc/<name>/`:

1. **Start** the PAI's runtime (LLM session, shell tool, event
   inbox tail). Write `pid`, set `status=running`, append to
   `log.md`.
2. **Wait for nudges**. The kernel routes events to the supervisor
   via the in-process bus; each nudge becomes a user turn.
3. **Construct the user turn**: event payload(s), recent context,
   any pending IPC. Hand to `llm.py` to generate the assistant
   response. Stream tool calls.
4. **Capture failures**. Tracebacks land in `/proc/<name>/log.md`.
   On unhandled exception, set `status=failed` and emit
   `kernel:proc_failed` (root catches this).
5. **Restart per policy** (`restart: never|on-failure|always`).
   Kernel restart counts as implicit failure → `on-failure` and
   `always` PAIs resume across kernel bounces.

Source: `/usr/src/boot/supervisor.py`, `/usr/src/boot/proc_watcher.py`.

## Nudge construction

`/usr/src/boot/nudge.py` is where a kernel event becomes a user
turn for the target PAI. It assembles:

- the event block (kind, source, payload)
- recent log tail for context
- any queued IPC the PAI hasn't seen yet
- the operating preamble

It does **not** carry conversation history across nudges — each
nudge is a fresh LLM call. Files are the source of truth; the PAI
re-reads what it needs each turn. (Persistent sessions are TODO;
see `memory/doc/KERNEL.md` §TODO.)

## Subagent supervision

A spawned subagent has `persistent: true` in its spec. The kernel
supervises it the same way — except:
- It only resolves on `bin/subagent done` (ephemeral) or parent
  shutdown (persub).
- Its events route point-to-point: parent IPC arrives as
  `pai_message`; child replies arrive at the parent as
  `subagent:response`.

## Diagnosing a stuck process

```sh
cat /proc/<name>/status                # what the kernel thinks
cat /proc/<name>/pid                   # POSIX pid
ps -p $(cat /proc/<name>/pid)          # is it actually running?
tail -n 50 /proc/<name>/log.md         # what was the last thing it did
```

Common patterns:
- `running` but pid is dead → kernel didn't notice the exit; emit
  `kernel:reload_config` to re-reconcile.
- `failed` with a Python traceback in `log.md` → use skill
  `diagnose-crash`.
- `running` but never replies → check the LLM provider/model in
  `/proc/<name>/spec.yaml`; check for stuck tool calls in `log.md`.

## Read these next

- `/usr/src/boot/supervisor.py` — the supervision loop.
- `/usr/src/boot/nudge.py` — turn construction.
- `/usr/src/boot/processes.py` — spawn/resolve.
- Skill `diagnose-crash` — classify a failure cause.
- Skill `understand-proc-services` — `/proc/<slug>/` file layout.
