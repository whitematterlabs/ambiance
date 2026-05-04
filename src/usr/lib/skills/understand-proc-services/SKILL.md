---
name: understand-proc-services
description: Process directory anatomy — spec.yaml shapes, status values, log format, restart policy. Read before reasoning about a /proc/<slug>/ entry.
---

# /proc/<slug>/ services

Every entry in `/proc/` is a kernel-supervised unit of work. There
is **no `type:` field** — the *shape* of a service is determined by
which fields are present in `spec.yaml`.

## File layout

```
/proc/<slug>/
├── spec.yaml      # service definition (managed fields from config + service shape)
├── pid            # POSIX pid of the running supervisor (if running)
├── status         # one word: spawned | running | completed | expired | cancelled | failed
└── log.md         # append-only [HH:MM]-prefixed log; subprocess stdout/stderr tee'd in
```

## Service shapes (no `type:` — determined by fields)

| Shape | Fields present | Behavior |
|---|---|---|
| **Background service** | `run:`, no `schedule:` | Kernel forks immediately, supervises until exit/cancel. On exit, resolves and emits an event. |
| **Reminder** | `schedule:`, no `run:` | Kernel arms a timer. On fire: nudges PAI with `reason: schedule fired`. |
| **Cron job** | `schedule:` and `run:` | On each fire, kernel launches a transient per-fire subprocess. Parent stays `running`. |
| **Deferred background** | `schedule:` (one-shot ISO datetime) + `run:` | At fire time, starts subprocess under supervision. |
| **Deadline-only** | `deadline:`, no `schedule:`/`run:` | Kernel auto-expires + nudges at deadline. **Deprecated** — prefer `schedule:` with ISO datetime. |
| **PAI process** | `kind: pai`, plus `provider`, `model`, `prompt`, `wake_on`, etc. | Long-running PAI supervised across kernel restarts. |
| **Persub** | PAI shape + `persistent: true`, `persub: true`, `parent: <pid>` | Long-lived child of another PAI. See `understand-persubs`. |

## Spec field reference (services)

```yaml
run: bin/subagent "research flights"   # background subprocess
restart: never                         # never | on-failure | always
deadline: 2026-04-22T20:00:00          # ISO datetime; kernel kills + resolves
schedule: "0 9 * * *"                  # cron expr OR one-shot ISO datetime
spawned: 2026-04-22T14:00:00           # stamped by paicron
description: "Dinner with kaia at 8"   # free text
people: [kaia]                         # related people
```

## status values

Single word, no YAML. `cat status` to read; `echo running > status`
to write.

- `spawned` — created, not yet running
- `running` — actively supervised
- `completed` — exited cleanly (rc=0)
- `expired` — deadline hit
- `cancelled` — explicitly stopped
- `failed` — non-zero exit, kernel restart of `restart: never`,
  unhandled exception

## Restart policy

- `never` (default) — exit resolves the proc.
- `on-failure` — re-fork on non-zero exit. Kernel restart counts
  as implicit failure → resumes across kernel bounces.
- `always` — re-fork on every exit.

## Spawning

`bin/paicron start --slug <s> --run "<cmd>" --restart <pol>` is the
ergonomic frontend. It writes `spec.yaml`, `status`, `log.md`, and
the kernel's `proc_watcher` picks up the new directory.

`paicron` auto-suffixes the slug with `-YYYY-MM-DD` (or full
timestamp on same-day collision). For PAI lifecycle (start/stop a
fleet member), use `paictl` — see skill `kernel-tools`.

## Read these next

- `memory/doc/KERNEL.md` §"Process Directory" / "Service shapes"
- `/usr/src/boot/processes.py` — spawn/resolve.
- `/usr/src/boot/supervisor.py` — supervision loop.
- Skill `kernel-tools` — paicron/paictl/paiman/paiadd/paidel.
