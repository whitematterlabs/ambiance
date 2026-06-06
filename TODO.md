# PAI roadmap

The v3 FHS migration is complete. Source of truth for layout is
`src/usr/share/doc/FILESYSTEM_v3.md`.

## Done

- Full FHS skeleton at `$PAI_ROOT` (defaults to `~/.pai/`), provisioned by
  `paifs-init` (boot, etc, usr, var, proc, sys, run, sbin, bin, home,
  root, opt, mnt, tmp, dev).
- Kernel decomposed into `src/boot/` with phased init: sanity → clean →
  probe → reconcile → start → entry. `/sbin/init` is the entrypoint.
- Three-location driver split: config in `/etc/drivers/<name>/`, code in
  `/usr/lib/drivers/<name>/`, runtime state in `/sys/drivers/<name>/`.
- `/proc/<pai>/` namespacing for multi-PAI service supervision.
- Tooling: `paiman init` (bundles), `paiadd` / `paidel` (configure
  instances), `paictl` (instance runtime via `active:` flag),
  `paicron` (services, the cron/systemctl analogue).

## Next

1. **`/opt/` bundle stitching.** Make `paiman install <url>` real:
   clone to `/opt/<pkg>/<ver>/`, resolve declared deps, expose drivers
   and skills via `/usr/lib/`. Unlocks shipping bundles via git.
2. `paiman` verbs beyond `init`: `install`, `uninstall`, `upgrade`,
   `list`. Depends on (1).

## Runtime bugs — triage 2026-06-06

Found by log/proc forensics while the kernel was down (stale pidfile, see B5).
Mail and subagent-reporting issues are tracked separately and excluded here.
Severity: P0 = system-breaking now, P1 = actively degrading, P2 = contained.

- [x] **B0 (P0) — context overflow / nudge storm — FIXED (`src/boot/nudge.py`).**
  Two kernel-side fixes + tests in `tests/test_compaction.py`:
  (1) reactive overflow recovery — on a provider context-window 400, archive
  the oversized history to `*-overflow.jsonl`, reset the conversation, and retry
  the turn once (self-calibrates to the real provider limit; no model
  cooperation needed); (2) storm gate — transient/systemic errors (connection,
  timeout, rate limit, overflow) are logged and dropped instead of re-nudging
  root per-failure. Genuine actionable failures still escalate to root.
  Original analysis below.

- [~] **B0 (P0) — `pai` context exceeds the 1M window; nudge storm.**
  Symptom: first failure is a hard `BadRequestError: maximum context length
  1048565, requested 1051452`. Once over, every nudge 400s; backlogs pile up
  and get mass-cancelled (`cancelling 10 → 17 → 30 nudge(s) for pai=2`); then
  194 `APIConnectionError`/`APITimeoutError` cascade (deepseek choking on
  ~1M-token requests, likely secondary not primary).
  Suspected cause: context grows unbounded across turns — no compaction/window
  before nudge assembly; failed nudges re-enqueue instead of backing off.
  Fix needs: (a) cap/compact turn history before send; (b) on over-limit, fail
  the nudge *terminally* — don't re-enqueue a request that can't fit;
  (c) coalesce/backpressure so nudges don't stack to 30. Confirm in
  `src/boot/nudge.py` (already in working tree).

- [ ] **B1 (P1) — triplicate identical `pai` instances amplify load 3×.**
  Symptom: `config.yaml` declares `pai`, `pai-2`, `pai-3` — all `active`, all
  `deepseek-v4-pro`, all "owner-facing catch-all for unclaimed events." Every
  unclaimed event nudges all three → 3× nudges + context growth feeding B0.
  Suspected cause: leftover experiment; three catch-alls with identical
  `wake_on`.
  Fix needs: decide intended fleet. If single catch-all → remove pai-2/pai-3
  from config + `paidel`. If multi-pai intended → give them distinct
  `wake_on`/routing so they don't all fire on every event.

- [ ] **B2 (P1, HELD) — nightly librarian cron broken + orphan accumulation.**
  Root cause confirmed: the cron's whole job is to put `librarian:consolidate`
  on the event bus, but **no CLI emits a raw event** — `send-message` only
  emits `pai_message` (needs `--to/--content/$PAI_PID`), so the canonical hook
  command `bin/send_message emit librarian:consolidate` can never work
  (`rc=127`/`rc=2`). The dated-suffix orphans (`-05-31/-06-01/-06-02`) come
  from the PAI self-healing via `paicron start` (auto-appends a date) instead
  of `ensure`.
  **Decision (2026-06-06): schedule-only reminder-nudge.** Drop `--run` from
  the canonical hook (pairegistry `pais/librarian-pai/package.yaml`); a
  schedule-only cron with `--parent-slug librarian-pai` nudges the librarian
  directly at 3 AM (reason "schedule fired", not "librarian:consolidate") — no
  new kernel surface. Still need to: reap the orphan dated procs and confirm
  the librarian prompt handles a generic schedule-fired wake.

- [x] **B4 — stale lifecycle state — CLOSED, non-issue.**
  `run/kernel.pid` is an flock lock (`boot/entry.py`); flock releases on death
  and boot `ftruncate`+rewrites the PID on every acquire. The stale `3439` is a
  harmless breadcrumb, and liveness checks use the flock/`lsof`, not the file
  contents. No fix needed.

- [~] **B3 (P2) — `voice-in` driver dead, missing venv deps.**
  Code fix DONE (pairegistry `drivers/voice/inbound.py`): heavy native deps
  (numpy/sounddevice/openwakeword/onnxruntime/whisper) are now guarded at
  import; `run()` degrades cleanly with an actionable "not provisioned" log +
  event instead of crashing the module load (which marked the proc `failed` and
  nudged root). Returns cleanly rather than raising, so no restart-storm.
  Current runtime state: `numpy` present; `sounddevice/openwakeword/onnxruntime/
  webrtcvad/soundfile` and `whisper-cli`/model all MISSING — voice is genuinely
  unprovisioned (`active: false`). The stale `failed` proc status is cosmetic.
  REMAINING (owner action): run `paiman install voice` to deploy the code fix
  AND provision (brew portaudio + build whisper.cpp + pip deps + ONNX models) —
  heavy/machine-specific, deliberately not auto-run. Follow-up worth
  considering: split the driver's lightweight pip deps into a paiman-managed
  manifest so a venv rebuild doesn't silently drop them.

- [ ] **B4 (P2) — stale lifecycle state on shutdown.**
  Symptom: `run/kernel.pid` = 3439 (dead) — stale pidfile not cleared on
  SIGTERM; `librarian-nightly` + orphan still report `status: running` while
  the kernel is down.
  Fix needs: liveness-check/clear stale pidfile on boot; on shutdown reconcile
  non-cron statuses to stopped; distinguish "preserved cron" from "running."

## Deferred

- Privileged-write enforcement (capability system: kernelPAI as the sole
  writer to `/etc/`, `/usr/`, `/opt/`, `/var/lib/memory/`; workers route
  through `/root/inbox/` elevation).
- Jailing (`/home/<pai>/` and `/root/` as enforced sandboxes).
- Modular kernel composition.
