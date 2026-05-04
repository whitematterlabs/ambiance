# Kernel-emitted events

Drivers ship an `events.yaml` manifest declaring the event kinds they
produce. The kernel itself also emits a small set of events at runtime.
This file is the analogue of `events.yaml` for kernel-origin events.

Events live as YAML files under `home/events/` and route to PAIs via
`wake_on:` globs in `/etc/config.yaml`. See `KERNEL.md` for the routing
machinery.

## Kinds

### `kernel:reload_config`

Emitted by `paictl` (via `/sbin/reboot` or `paictl start/stop <slug>`)
when `/etc/config.yaml` changes or a fleet member's `active:` flag is
flipped. Tells the kernel to reconcile `/proc/<pai>/spec.yaml` against
config and start/stop instances accordingly.

```yaml
source: kernel
kind: kernel:reload_config
```

### `proc_resolved`

Emitted when a process transitions to one of `completed`, `expired`, or
`failed` (cancellation is excluded — see `processes.NUDGE_ON_RESOLVE`).
Routes to the resolved process's `parent` so it can react to its child
finishing.

```yaml
source: kernel
kind: proc_resolved
slug: <child-slug>
status: completed | expired | failed
parent: <parent-pid>      # present when the spec declares one
```

### `pai:<slug>:input`

Emitted at the start of every nudge, after the target PAI's slug/pid is
resolved but before the LLM runs. Lets listeners react to *what woke*
another PAI without changing the kernel. Carries the `reason` string
and (when present) the originating event/context as `trigger`.

```yaml
source: pai
kind: pai:<slug>:input
slug: <slug>
pid: <int>
reason: <str>
trigger:                  # optional; the event/context that caused the nudge
  ...
```

### `pai:<slug>:output`

Emitted immediately after a nudge commits the assistant reply to
`proc/<slug>/messages.jsonl`. Pointer-style: subscribers re-read the
file themselves. `turn_index` is the length of the history after save,
so the just-appended assistant turn is at line `turn_index` (1-indexed).

```yaml
source: pai
kind: pai:<slug>:output
slug: <slug>
pid: <int>
turn_index: <int>
messages_path: proc/<slug>/messages.jsonl
```

## Subscribing

A listener PAI in `/etc/config.yaml`:

```yaml
- name: memory
  wake_on:
    - pai:main:output     # specific slug — recommended
```

### Loop hazard

`wake_on: [pai:*:output]` will self-trigger when the listener itself
produces a turn (its own `:output` matches the glob). Always target
specific slugs unless you have an explicit reason to fan out across the
whole fleet *and* a guard against re-entry.
