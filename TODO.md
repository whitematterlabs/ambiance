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

## Done (since the original roadmap)

- **`/opt/` bundle stitching — DONE.** `paiman install <name|path|git-url>`
  stages into `/opt/<pkg>/<ver>/` and exposes drivers/skills/pais via
  `/usr/lib/`. The companion `pairegistry` is the canonical package source.
- **`paiman` verbs beyond `init` — DONE.** `install`, `remove`, `list`,
  `show` all ship (see `src/bin/paiman.py`).
- **Context-limit compaction & session restart — DONE.** Soft per-PAI
  compaction threshold plus a kernel-enforced hard backstop in
  `src/boot/nudge.py`; overflow recovery archives oversized history and
  retries the turn.
- **Session persistence across nudges — DONE.** Per-PAI conversation
  history lives in `proc/<pai>/messages.jsonl`, threaded through each turn
  and persisted on completion.

## Next

No engine-level roadmap items are open. Remaining work is launch hygiene
(license, clean-install verification, doc sync) and the userspace packages
shipped from `pairegistry`. See **Deferred** for post-launch hardening.

## Runtime bugs — triage 2026-06-06

Found by log/proc forensics while the kernel was down (stale pidfile, see B4).
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

- [x] **B1 (P1) — clones amplified load by auto-inheriting wakes — FIXED.**
  Root cause: `paiclone.plan_clone` shallow-copied the source entry, dragging
  `wake_on` along — so `pai-2`/`pai-3` (clones of `pai`) inherited the
  catch-all subscription and every unclaimed event nudged all three. Fix:
  clones no longer inherit wakes — `plan_clone` drops `wake_on`, so a fresh
  clone is inert until the owner assigns it routing explicitly. Covers both
  surfaces (CLI `paiclone` + web "clone" button, both via `paiclone.clone`).
  Test `test_web_clone.py` flipped to assert no inherited `wake_on`.
  (Cleaning up the existing pai-2/pai-3 entries is owner action: `paidel`
  them, or give them distinct `wake_on` if multi-pai is intended.)

- [x] **B2 (P1) — nightly librarian cron broken + orphan accumulation — FIXED.**
  Root cause: the cron's job is to put `librarian:consolidate` on the event
  bus, but **no CLI emits a raw event** — `send-message` only emits
  `pai_message` — so `bin/send_message emit librarian:consolidate` could never
  work (`rc=127`/`rc=2`). Dated-suffix orphans came from `paicron start`
  (auto-appends a date) instead of `ensure`.
  Fix (schedule-only reminder-nudge, pairegistry `pais/librarian/`):
  dropped `--run` from the boot hook; a schedule-only cron with
  `--parent-slug librarian` nudges the librarian directly at 03:00
  (reason "schedule fired") — no new kernel surface. Added a `--description`
  carrying intent and updated `prompt.md` to treat a scheduled wake as the
  nightly run. `ensure` keeps the slug stable so dated dupes won't recur.
  Reaped the runtime orphan `/proc/librarian-nightly-2026-06-02` (its log
  confirmed schedule-fired → nudge complete, validating the approach). The
  stale `librarian-nightly` proc will be replaced by `ensure` on next boot
  (spec now differs).

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

- [x] **B4 (P2) — stale lifecycle state on shutdown — FIXED.**
  Three sub-points, all resolved:
  (1) *Stale pidfile* — non-issue (see the closed B4 above): `run/kernel.pid`
  is an flock lock; liveness uses the lock, not the file contents.
  (2) *Reconcile non-cron statuses to stopped on shutdown* — already done by
  the shutdown sweep (`main.py`), verified live (root/pais → `stopped`,
  drivers → `cancelled`).
  (3) *Distinguish "preserved cron" from "running"* — DONE. Armed timers
  (cron/deadline/one-shot) now rest at a new `scheduled` status instead of
  masquerading as `running`. `scheduled` joins `running` as the kernel's
  *active* set; re-arm (`timers.rebuild_from_proc`), fire (`_handle_timer`),
  proc-watcher heap sync, and the shutdown sweep all key off the active set,
  not `running` alone. Initial status is computed from the spec
  (`processes.is_timer`); a deferred one-shot service flips `scheduled`→
  `running` when the supervisor starts it. Surfaces (TUI/web) enumerate via
  `list_active_procs()` so resting crons stay visible. `paicron`'s existing
  `scheduled` half-stub is now actually produced. Tests in
  `tests/test_cron_status.py`; docs in `KERNEL.md`.

## Deferred

- Privileged-write enforcement (capability system: kernelPAI as the sole
  writer to `/etc/`, `/usr/`, `/opt/`, `/var/lib/memory/`; workers route
  through `/root/inbox/` elevation).
- Jailing (`/home/<pai>/` and `/root/` as enforced sandboxes).
- Modular kernel composition.
