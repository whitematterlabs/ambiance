# Kernel Architecture (v3)

The authoritative spec is `FILESYSTEM_v3.md`. This is the operator's
quick map — what lives where, who owns what, how events flow.

## Layering rule (load-bearing)

- **`/boot/`** — kernel image. PID 1's supervisor + every helper it
  links against. Pure Python. Source: `<repo>/src/boot/`.
- **`/usr/`** — userspace. Drivers, skills, PAI bundles, shipped data.
  Never holds kernel code.
- **`/sbin/`** — kernelPAI-only tools + `/sbin/init` (entrypoint).
- **`/bin/`** (and `/usr/bin/`) — PAI-callable tools (`paictl`, `paicron`, etc.).

If something owns the on-disk shape of an external surface (messages,
email, contacts), it is a **driver**, not kernel.

## Three-location driver split

Every driver fans across two FHS slots, plus its `/proc/` entry:

| Slot | Holds |
|---|---|
| `/usr/lib/drivers/<name>/` | Source code + shipped `events.yaml` manifest |
| `/sys/drivers/<name>/` | Live runtime state — cursors, last event |
| `/proc/<slug>/` | Kernel-managed lifecycle (status, log, `active:` flag) |

Source lives in **`~/Projects/pairegistry/drivers/<name>/`** (NOT in this pyproject repo) and is installed into `/usr/lib/drivers/` by `paiman install <name>`. There is no `/etc/drivers/`: drivers are a code-time registry in the kernel, not user-editable config.

### Driver runtime contract

A driver process is **not** a `/proc/<slug>/spec.yaml`-style fork-and-supervise child. It is an **in-process asyncio task** living inside the kernel itself. Reconcile reads each driver's `events.yaml`, imports the listed module, calls `<module>.<entrypoint>()` to get a coroutine, and schedules it under `_supervise_driver`.

Consequences the author-driver skill expands on:

- The entrypoint **must be `async def`**. A sync `def run()` with `while True: time.sleep(N)` enters its loop on the kernel's main thread when reconcile calls it, never returns, and wedges every other driver and every PAI nudge until the kernel is killed.
- Blocking I/O inside an async driver (sync `requests`, `subprocess.run`, blocking `sqlite3`) freezes the event loop for the duration of the call. Wrap such calls in `asyncio.to_thread`, or use the asyncio-native equivalents.
- Driver tasks are cancelled on shutdown and on `paictl stop <slug>`. Honor `asyncio.CancelledError` for cleanup.

See the `author-driver` skill for the full contract and reference skeletons.

## Bundle / instance / process

- **Bundle** (template): `/opt/<pkg>/<ver>/` (release) or
  `/usr/lib/pais/<name>/` (dev source).
- **Instance** (configured PAI, sacred state): `/var/lib/instances/<pai>/`.
- **Process** (running PAI): `/proc/<pai>/`.

Stitched home view:
- pid 1 (root) lives at `/root/`.
- every other PAI lives at `/home/<slug>/`.

## Reserved PIDs

- `1` → `root` — kernel-internal events, errored nudges, fallback.
- `2` → `pai` — owner-facing PAI, catch-all.

Auto-allocated PIDs are invariant once assigned.

## Event flow

1. A driver writes an event file under `/var/log/events/` (or the live
   events dir) with a `kind:` field.
2. The kernel matches `kind` against every PAI's `wake_on:` glob.
3. Every match is nudged (fan-out). Zero matches → all `fallback: true`
   PAIs. Still zero → root (pid 1).
4. The receiving PAI gets the event in its user turn; it acts.

The kernel does not know what a "message" is. On-disk shape decisions
belong to drivers.

## Source-of-truth files

- `/etc/config.yaml` — fleet declaration. Reconcile rewrites
  `/proc/<pai>/spec.yaml` from it on boot and on `kernel:reload_config`.
- `/usr/lib/drivers/<name>/events.yaml` — every kind a driver may emit.
  This is the routing vocabulary — match `wake_on` globs against it.
- `/usr/src/boot/config.py` → `CONFIG_MANAGED_FIELDS` is the schema
  authority for what reconcile manages vs. preserves on `spec.yaml`.

## Process layout

`/proc/<pai>/`:
- `spec.yaml` — last reconciled spec (managed fields rewritten;
  others preserved).
- `pid` — POSIX pid of the running supervisor.
- `status` — `running` / `failed` / `stopped`.
- `log.md` — append-only operational log (tracebacks land here).
