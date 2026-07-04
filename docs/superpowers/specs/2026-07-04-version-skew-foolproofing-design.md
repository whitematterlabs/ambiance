# Version-skew foolproofing — design

**Date:** 2026-07-04
**Status:** approved, implementing

## Problem

`pai update` (tarball path) repoints the `opt/pai/current` symlink and rebuilds
web assets, but restarts **neither** running process. The kernel keeps running
its old build until a separate manual `sbin/reboot`; the web console keeps
running its old build until a manual `pai start --web`. `pai start` correctly
no-ops an already-running kernel (`kernel already running; exiting`), so an
operator who runs `update` then `start` believes they are live while the kernel
is still on the old build.

This produced a silent split-brain: after the me-thread transcript key changed
from pid to slug (commit cc9901d), a **new web** wrote owner messages to
`me/<slug>/` while the **old kernel** wrote replies to `me/<pid>/`. Owner saw
their question with no answer — "PAI is answering (activity visible) but the
chat isn't updating."

Root class: there is no single atomic "adopt the new build" operation, and no
runtime signal of which build each component is actually running, so skew is
both easy to cause and invisible.

## Goals

1. **Prevent** the common skip: `pai update` makes the running system fully live.
2. **Detect + auto-heal** any residual skew loudly, never silently.

## Non-goals

- Co-supervising the web under the kernel (surfaces attach, don't own the
  runtime). The web stays a separate process; it just becomes build-aware.
- Backward/forward-compatible on-disk formats for every future change. Detection
  + fast auto-reboot shrink the skew window instead.

## Design

### 1. Build identity — `src/boot/build.py`

`running_build() -> Build` where `Build = {version: str, sha: str | None, dev: bool}`.

- Derives `version` by walking `__file__` up to a `.../opt/pai/<ver>/` ancestor;
  `<ver>` is the version. A process cannot misreport the code it imported —
  this is ground truth, immune to `.release` / symlink drift.
- Not under `opt/pai/` (dev / git checkout) → `{dev: true, version: "dev",
  sha: <git HEAD short>}`.
- `sha`: from the installed sha marker when present, else git HEAD.

This is the single identity every comparison uses.

### 2. Kernel stamps itself at boot — `run/pai/build/kernel.json`

At kernel start, write `{version, sha, pid, started}`. Because `kernel:restart`
re-execs through boot, an in-place re-exec **restamps automatically** to the new
build. Overwritten each boot; a reader treats a stamp whose `pid` no longer
holds the kernel lock as stale.

### 3. Web publishes both builds

- The web server computes its own `running_build()` at startup.
- The hub reads `kernel.json` (watched) and includes
  `{kernel_build, console_build, current_release}` in the SSE `hello` snapshot,
  plus a `build` message when it changes. `current_release` = readlink
  `opt/pai/current` (the installed/target build; `dev` when no symlink).

### 4. Skew detection + guarded auto-heal

Pure decision function (host-testable), evaluated on the client from the three
build values:

- `kernel.version == console.version` → in sync, no action.
- **Kernel stale** (`console == current_release` and `kernel != current_release`)
  → emit `kernel:restart` once; toast "Kernel on old build — rebooting to
  `<current_release>`".
  - **Loop guard:** remember `(kernel.version, target, ts)` of the last attempt.
    No re-emit within a 60s cooldown. If after the cooldown the kernel is still
    on the same stale version, stop auto-healing and show a persistent manual
    banner: "auto-reboot didn't take — run `sbin/reboot`".
- **Console stale** (`console != current_release`) → warn-only banner "console
  on old build — restart `pai start --web`". Never auto-act (rebooting the
  kernel can't fix a stale console).

The heal action goes through the existing web→kernel event channel: new
`actions.reboot_kernel()` emits the same `{kind: "kernel:restart", source:
"web"}` payload as `sbin/reboot`, guarded server-side to fire only when a kernel
holds the lock.

### 5. Prevention — `pai update` auto-restarts the kernel

After `_repoint_current` + markers (both tarball and dev reprovision paths), if
a kernel is running (non-blocking flock probe on `run/kernel.pid`, same test as
`sbin/reboot`), emit `kernel:restart`. Print "kernel restarting into `<ver>`".
`--no-restart` stages the build without going live.

A stale **console** after update is caught by its own banner (§4), so both
components are covered without update having to reach into the detached web
process.

## Testing

- `build.running_build`: version derived from a fake `__file__` planted under
  `opt/pai/<ver>/src/...`; dev fallback when outside `opt/pai/`.
- Kernel boot writes `kernel.json` with the running build + pid.
- Hub snapshot carries `{kernel_build, console_build, current_release}`.
- Heal decision as a pure function: in-sync → none; kernel-stale → reboot;
  within cooldown → none; still-stale after cooldown → escalate/banner;
  console-stale → warn-only.
- `pai update` emits `kernel:restart` when a kernel is running; `--no-restart`
  suppresses it.

## Files touched

- new `src/boot/build.py` (+ dual-home nothing; kernel code)
- `src/boot/entry.py` or main startup — write `kernel.json`
- `src/usr/libexec/web/pai_web/{server,hub,actions}.py` — publish builds, heal action
- `src/usr/libexec/web/src/App.tsx` (+ a small component) — banner / toast / auto-heal
- `src/bin/pai.py` — auto-restart after update, `--no-restart`
- tests under `tests/`
