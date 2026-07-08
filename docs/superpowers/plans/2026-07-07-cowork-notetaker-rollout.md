# Cowork Mode + Notetaker Rollout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement and roll out the `cowork` driver (window/clipboard/file activity, capability default **yes**) and the `notetaker` driver (local call recording + transcription, capability default **no**) per the two specs in `docs/superpowers/specs/`, live on the owner's machine.

**Architecture:** Kernel gains per-flag capability defaults + restricted mode sets (`no`/`yes` for capture flags) in `CAPABILITY_SPECS`; enforcement reuses the existing `project_capabilities` freeze-file projection (`sys/drivers/<name>/capture.freeze`, presence = capture disabled). Two new registry driver bundles follow the calendar driver's dedicated-runloop-thread pattern (drivers are asyncio tasks **inside the kernel process** — never block the loop). Events ride the generic `source:kind` router; no kernel routing changes needed.

**Tech Stack:** Python 3.14, pyobjc (AppKit, Quartz — installed; ApplicationServices, FSEvents, CoreAudio — to add), sounddevice/soundfile (installed via voice), whisper.cpp via `drivers.voice.stt`, watchdog, React/Vite web console.

## Global Constraints

- Specs: `docs/superpowers/specs/2026-07-03-cowork-window-activity-design.md`, `2026-07-04-notetaker-driver-design.md`. They are authoritative; deviations listed below.
- Driver source lives in `~/Projects/pairegistry/drivers/<name>/`, NOT in the pai repo (CLAUDE.md hard rule).
- Kernel + web changes live in `~/Projects/pai` (`src/boot/`, `src/usr/libexec/web/`).
- Tickless dogma: no polling loops; every wait is on an FS event, queue, or OS push callback. Runloop threads pump in bounded slices with the calendar busy-spin guard.
- Drivers run **inside the kernel process** as asyncio tasks. `run()` is a zero-arg async coroutine; re-raise `CancelledError`; teardown in `finally:`.
- ObjC observer classes must be defined once per process and cached (module-global), or driver restart crashes (`objc` class redefine).
- Prompts/prose: say "the owner", never a name. PAI-facing paths are FHS paths (`sys/drivers/...`), never `~/.pai/...`.
- Log via `print(..., flush=True)` (kernel tees to kernel.log).
- Commit + push after every task; `uv run pairelease --publish` at rollout.
- Resolve the real OS home via `pwd.getpwuid(os.getuid()).pw_dir`, never `$HOME` (PAI-sandbox lesson).
- Deviations from spec (accepted, documented here):
  - Idle-seconds via `Quartz.CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, kCGAnyInputEventType)` instead of IOKit `HIDIdleTime` — same signal, zero new deps/permissions (pyobjc has no IOKit wrapper).
  - Clipboard/event payload text is truncated to 2,000 chars in the **emitted event** (`truncated: true` marker); the ndjson log stores up to 100,000 chars. Megabyte pastes must not blow up PAI prompts.
  - Notetaker records raw PCM (`audio.raw`, s16le) finalized to WAV via ffmpeg, not `.caf` — crash-safe (a dead process leaves a valid PCM stream; `.caf`/WAV headers written on close are not). Same privacy semantics: deleted on successful transcription.

---

### Task 1: Kernel capability specs — per-flag defaults + restricted modes (TDD)

**Files:**
- Modify: `~/Projects/pai/src/boot/config.py` (CAPABILITY_SPECS at :60, `capability_modes` at :512, `set_capability_mode` at :527)
- Test: `~/Projects/pai/tests/test_config.py`

**Interfaces:**
- Produces: `CAPABILITY_SPECS["cowork"] = {"driver":"cowork","freeze":"capture.freeze","mounts":{"cowork"},"default":"yes","modes":("no","yes")}` and `CAPABILITY_SPECS["notetaker"] = {"driver":"notetaker","freeze":"capture.freeze","mounts":{"notetaker"},"default":"no","modes":("no","yes")}`. `capability_modes()` returns a complete dict for all 5 flags where an **absent key** resolves to `spec.get("default","no")` and any mode not in `spec.get("modes", CAPABILITY_MODES)` clamps to `"no"`. `set_capability_mode` rejects modes outside the flag's `modes`.
- `project_capabilities` (config.py:565) needs **no change**: mode `yes` → unlink freeze, else write it — now correct for capture flags too.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_config.py` (match the file's existing fixture style; existing capability tests at :654-:836 show the `repo_root` fixture + config-writing helpers to copy):

```python
def test_cowork_defaults_yes_when_key_absent(repo_root):
    # capabilities block exists but has no cowork key
    _write_config_with_capabilities(repo_root, {"email_send": "no"})
    modes = C.capability_modes()
    assert modes["cowork"] == "yes"
    assert modes["notetaker"] == "no"

def test_capture_flag_ask_clamps_to_no(repo_root):
    _write_config_with_capabilities(repo_root, {"cowork": "ask", "notetaker": "ask"})
    modes = C.capability_modes()
    assert modes["cowork"] == "no"
    assert modes["notetaker"] == "no"

def test_set_capability_mode_rejects_ask_for_capture_flags(repo_root):
    with pytest.raises(ValueError):
        C.set_capability_mode("cowork", "ask")

def test_project_capabilities_capture_freeze(repo_root, tmp_path, monkeypatch):
    # cowork absent (default yes) -> no freeze; notetaker absent (default no) -> freeze written
    _write_config_with_capabilities(repo_root, {})
    C.project_capabilities()
    from boot import paths
    assert not (paths.sys_drivers("cowork") / "capture.freeze").exists()
    assert (paths.sys_drivers("notetaker") / "capture.freeze").exists()
```

(`_write_config_with_capabilities` = whatever helper the existing tests at :768+ use — reuse it verbatim; if they inline YAML, inline the same way.)

- [ ] **Step 2: Run to verify failure** — `uv run python -m pytest tests/test_config.py -k "cowork or capture" -v` → FAIL (KeyError/assert).

- [ ] **Step 3: Implement** in `src/boot/config.py`:

```python
    # appended inside CAPABILITY_SPECS (after whatsapp_send):
    # Ambient-capture gates, not send freezes: the freeze file gates whether
    # the driver captures at all. `default` is the mode when the key is absent
    # from config.yaml (send flags stay fail-closed; cowork ships on-by-default
    # per its spec). `modes` restricts the tri-state — "ask" is meaningless for
    # capture, a capture either happens or it doesn't.
    "cowork": {
        "driver": "cowork", "freeze": "capture.freeze", "mounts": {"cowork"},
        "default": "yes", "modes": ("no", "yes"),
    },
    "notetaker": {
        "driver": "notetaker", "freeze": "capture.freeze", "mounts": {"notetaker"},
        "default": "no", "modes": ("no", "yes"),
    },
```

`capability_modes` (replace the final return + dict comprehension):

```python
    out: dict[str, str] = {}
    for k, spec in CAPABILITY_SPECS.items():
        if k in caps:
            mode = _normalize_capability_mode(caps.get(k))
        else:
            mode = spec.get("default", "no")
        if mode not in spec.get("modes", CAPABILITY_MODES):
            mode = "no"
        out[k] = mode
    return out
```

Missing-file/parse-error path at :521 stays `{k: "no" ...}` (fail closed — atomic config writes mean no torn reads in practice).

`set_capability_mode`: replace the `mode not in CAPABILITY_MODES` check with the flag's own set:

```python
    allowed = CAPABILITY_SPECS[flag].get("modes", CAPABILITY_MODES)
    if mode not in allowed:
        raise ValueError(f"capability {flag!r} accepts {allowed}, got {mode!r}")
```

- [ ] **Step 4: Fix the pre-existing exact-dict tests** — every assertion in `tests/test_config.py` (:654-:836) and `tests/test_web_send_mode.py` that equality-compares the full modes/flags dict now needs `"cowork": ...` / `"notetaker": ...` entries (cowork resolves `"yes"`/`True` when absent). Update them to the new truth; do not weaken them to subset checks.

- [ ] **Step 5: Full test run** — `uv run python -m pytest` → all green (baseline was 332 passed, 2 skipped).

- [ ] **Step 6: Commit** — `git -C ~/Projects/pai add -A && git commit -m "kernel: capability flags get per-flag defaults + restricted modes; add cowork/notetaker capture gates" && git push`

---

### Task 2: `<capabilities>` prompt prose + web console toggle

**Files:**
- Modify: `~/Projects/pai/src/boot/bootstrap.py` (`_CAPABILITY_LINES` at :576)
- Modify: `~/Projects/pai/src/usr/libexec/web/pai_web/actions.py` (`SEND_CHANNEL_LABELS` :803, `list_send_capabilities` :830)
- Modify: `~/Projects/pai/src/usr/libexec/web/src/types.ts` (:72), `src/components/SendPermissions.tsx`
- Test: `~/Projects/pai/tests/test_web_send_mode.py`

**Interfaces:**
- Consumes: Task 1's `CAPABILITY_SPECS` `modes` field.
- Produces: each capability row from `list_send_capabilities()` gains `"modes": list[str]`; frontend renders only those modes. `_CAPABILITY_LINES` gains `cowork`/`notetaker` entries (`yes`/`no` keys only).

- [ ] **Step 1: bootstrap prose** — add to `_CAPABILITY_LINES` (owner-generic wording, disclosure per spec):

```python
    "cowork": {
        "yes": (
            "Cowork Mode is ON: you receive live events for the owner's window "
            "focus (app, title, open URL/file, idle seconds), clipboard copies, "
            "and file activity across their home folder. The activity logs are "
            "at sys/drivers/cowork/*.ndjson — grep them to answer \"what was I "
            "doing at 2pm\". React to events only when genuinely useful; most "
            "switches deserve silence."
        ),
        "no": (
            "Cowork Mode is OFF: no window, clipboard, or file activity is "
            "being captured, and you cannot see what the owner is doing on "
            "screen."
        ),
    },
    "notetaker": {
        "yes": (
            "Notetaker is enabled: when the owner explicitly asks you to take "
            "notes on a call, write `action: start` (optionally `cloud: true`) "
            "as YAML to a new file under sys/drivers/notetaker/commands/ and "
            "announce that recording has started; write `action: stop` when "
            "they ask you to stop. You'll receive notetaker:transcript_ready "
            "with the transcript path — write the summary + action items to "
            "notes/calls/<date>-<slug>.md in your home. Never start recording "
            "unprompted; disclosure to other participants is the owner's call."
        ),
        "no": (
            "Notetaker is disabled: you cannot record calls. If the owner asks "
            "for call notes, tell them to enable the Notetaker capability in "
            "the console first."
        ),
    },
```

- [ ] **Step 2: backend rows** — `actions.py`: add `"cowork": "Cowork Mode", "notetaker": "Notetaker"` to `SEND_CHANNEL_LABELS`; in `list_send_capabilities` include the allowed modes in each row dict: `"modes": list(spec.get("modes", ("no", "ask", "yes")))`. Add a test in `tests/test_web_send_mode.py` asserting a cowork row carries `modes == ["no", "yes"]` when the cowork driver is mounted (monkeypatch `_mounted_driver_union` like the existing tests at :60-:75 do).

- [ ] **Step 3: frontend** — `types.ts`: `SendCapability` gains `modes?: SendMode[]`. `SendPermissions.tsx`: filter the hardcoded `MODES` per row: `const rowModes = MODES.filter((m) => !cap.modes || cap.modes.includes(m.value));` and map over `rowModes`. Add per-flag hint overrides so capture flags don't get send-centric copy:

```tsx
const FLAG_HINTS: Record<string, Partial<Record<SendMode, string>>> = {
  cowork: { yes: "PAI sees window, clipboard + file activity", no: "no ambient capture" },
  notetaker: { yes: "PAI may record calls when you ask", no: "call recording disabled" },
};
```

If the panel's visible heading says "Send", generalize it to "Permissions".

- [ ] **Step 4: build + test** — `cd ~/Projects/pai/src/usr/libexec/web && pnpm build` (tsc + vite; dist/ is committed). `uv run python -m pytest tests/test_web_send_mode.py -v` → PASS.

- [ ] **Step 5: Commit** — `git -C ~/Projects/pai add -A && git commit -m "web+bootstrap: cowork/notetaker capability toggles (two-state) + prompt disclosure" && git push`

---

### Task 3: Probe the two risky OS APIs (scratch scripts, no repo changes)

**Files:**
- Create (scratchpad only): `probe_nsworkspace.py`, `probe_tap.py`

**Interfaces:** Produces go/no-go facts the driver code in Tasks 4-6 depends on. **Do this before writing driver code.**

- [ ] **Step 1: install the new pyobjc frameworks into the FHS venv** (also needed at runtime later):
`uv pip install --python ~/.pai/usr/lib/venv/bin/python pyobjc-framework-ApplicationServices pyobjc-framework-FSEvents pyobjc-framework-CoreAudio`

- [ ] **Step 2: NSWorkspace off-main-thread delivery probe.** Script: dedicated `threading.Thread` that (a) registers an `NSObject` observer for `NSWorkspaceDidActivateApplicationNotification` on `NSWorkspace.sharedWorkspace().notificationCenter()` **from inside the thread**, (b) pumps `NSRunLoop.currentRunLoop().runUntilDate_` in 1s slices with the calendar busy-spin guard. Main thread sleeps 15s while you `open -a Calculator` / switch apps. Expected: activation lines print with app name + pid. If nothing arrives: retry with the observer registered on the main thread pumping the runloop there — if only-main works, the driver's watcher thread design must instead run the runloop on a thread that *first touches* NSWorkspace (document which variant worked; all variants stay off the asyncio loop).

- [ ] **Step 3: process-tap probe.** Script: `CATapDescription.alloc().initStereoGlobalTapButExcludeProcesses_([])`, `AudioHardwareCreateProcessTap` → expect status 0 + tap id (a TCC prompt/denial returns nonzero — note the code), then `AudioHardwareCreateAggregateDevice` with a composition dict containing `aggregate-device-tap-list: [{"tap-uid": <desc.UUID().UUIDString()>}]` plus the default input device's UID in `aggregate-device-sub-device-list`, then refresh sounddevice (`sd._terminate(); sd._initialize()`) and confirm the aggregate appears in `sd.query_devices()` and an `InputStream` opens and delivers nonzero frames while audio plays. Destroy tap + aggregate on exit (`AudioHardwareDestroyAggregateDevice`, `AudioHardwareDestroyProcessTap`). Record the exact working call signatures in the task notes — Task 6 copies them.

- [ ] **Step 4:** No commit (scratch only). If either probe hard-fails with no variant working, STOP and surface to the owner — the affected driver's design premise is broken (fallbacks: `axd` sidecar for activations; ScreenCaptureKit audio for the tap).

---

### Task 4: cowork driver — bundle, manifests, shared helpers, window/clipboard tracker

**Files:**
- Create: `~/Projects/pairegistry/drivers/cowork/__init__.py` (empty)
- Create: `~/Projects/pairegistry/drivers/cowork/package.yaml`
- Create: `~/Projects/pairegistry/drivers/cowork/events.yaml`
- Create: `~/Projects/pairegistry/drivers/cowork/common.py`
- Create: `~/Projects/pairegistry/drivers/cowork/window_activity.py`
- Create: `~/Projects/pairegistry/drivers/cowork/libexec/install.sh`

**Interfaces:**
- Consumes: `boot.paths.PAI_ROOT`, `boot.processes.emit_event`, freeze file from Task 1's projection.
- Produces: `common.capture_enabled() -> bool`, `common.append_ndjson(path: Path, obj: dict) -> None`, `common.now_iso() -> str`, `common.STATE_DIR`, `common.event_text(s: str) -> tuple[str, bool]` (2,000-char truncation for event payloads). Emits raw kinds `window_changed`/`clipboard_changed` with `source: cowork` → public `cowork:window_changed`/`cowork:clipboard_changed`. Slugs `cowork-window`, `cowork-files`.

- [ ] **Step 1: manifests.**

`package.yaml`:
```yaml
name: cowork
kind: driver
version: 0.1.0
description: "Cowork Mode: ambient window/app focus, clipboard copy-log, and home-tree file activity. Gated by the cowork capability (default on)."
hooks:
  install:
    - "bash usr/lib/drivers/cowork/libexec/install.sh"
```

`libexec/install.sh` (mirror voice's venv-detection preamble verbatim, then):
```bash
uv pip install --python "$VENV_PY" pyobjc-framework-ApplicationServices pyobjc-framework-FSEvents
```

`events.yaml` (follow imessage's commented style):
```yaml
driver: cowork
description: Cowork Mode — ambient window, clipboard, and file activity capture.

processes:
  - slug: cowork-window
    module: drivers.cowork.window_activity
    entrypoint: run
  - slug: cowork-files
    module: drivers.cowork.file_activity
    entrypoint: run

events:
  - kind: cowork:window_changed
    description: Owner focused a different app/window. Enriched best-effort
      with the browser URL or open file path, plus idle seconds at capture.
    emitted_by: drivers/cowork/window_activity.py
    raw_kind: window_changed
    payload:
      app: string
      window: string        # may be null (AX denied / no title)
      pid: int
      ts: string            # ISO8601 UTC
      idle_seconds: float
      url: string           # browsers only, best-effort
      file_path: string     # editors/viewers only, best-effort
  - kind: cowork:clipboard_changed
    description: Pasteboard changeCount moved since the last app switch
      (event-driven copy-log; copies without a subsequent switch are missed
      by design).
    emitted_by: drivers/cowork/window_activity.py
    raw_kind: clipboard_changed
    payload:
      app: string           # frontmost app now — best-effort attribution
      content: string       # null for non-string pasteboards; truncated at 2000
      truncated: bool
      type: string          # "string" | "file-url" | "image" | "other"
      ts: string
  - kind: cowork:file_changed
    description: A file under the owner's home changed (raw FSEvents flags,
      not a classified operation). Denylist keeps this human-paced.
    emitted_by: drivers/cowork/file_activity.py
    raw_kind: file_changed
    payload:
      path: string
      change: list          # subset of [created, removed, renamed, modified]
      source_url: string    # kMDItemWhereFroms xattr, when present
      ts: string
```

- [ ] **Step 2: `common.py`** — complete file:

```python
"""Shared helpers for the cowork driver's tracker processes."""
import json
from datetime import datetime, timezone
from pathlib import Path

from boot import paths

STATE_DIR = paths.PAI_ROOT / "sys" / "drivers" / "cowork"
FREEZE_PATH = STATE_DIR / "capture.freeze"

NDJSON_TEXT_CAP = 100_000   # per-line safety cap in the on-disk log
EVENT_TEXT_CAP = 2_000      # cap for text carried inside a kernel event


def capture_enabled() -> bool:
    """Cowork Mode gate: the kernel projects capabilities.cowork into
    capture.freeze (presence = disabled). Cheap stat, checked per event."""
    return not FREEZE_PATH.exists()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_ndjson(path: Path, obj: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def event_text(s: str) -> tuple[str, bool]:
    """Truncate text destined for an event payload (prompt-bound)."""
    if len(s) <= EVENT_TEXT_CAP:
        return s, False
    return s[:EVENT_TEXT_CAP], True
```

- [ ] **Step 3: `window_activity.py`** — complete file. Structure (calendar's pattern, adapted per the Task 3 probe result):

```python
"""Cowork window/app-focus tracker + piggybacked clipboard copy-log.

One NSWorkspace didActivateApplication observer on a dedicated runloop
thread; each activation is bridged to the asyncio loop, enriched (AX window
title + document path, browser tab URL via AppleScript, idle seconds via
Quartz), logged to window_activity.ndjson, and emitted as cowork:window_changed.
On the same activation the pasteboard changeCount is sampled — if it moved,
the copy is logged to clipboard.ndjson and emitted as cowork:clipboard_changed.
No timers anywhere: this rides the OS push notification only.
"""
import asyncio
import subprocess
import threading
import time

from boot import processes as P
from drivers.cowork import common

WINDOW_LOG = common.STATE_DIR / "window_activity.ndjson"
CLIPBOARD_LOG = common.STATE_DIR / "clipboard.ndjson"
RUNLOOP_SLICE = 1.0

# AppleScript per browser for the frontmost tab URL. Best-effort: first use
# may raise an Automation TCC prompt; denial just means no `url` field.
_BROWSER_URL_SCRIPTS = {
    "Safari": 'tell application "Safari" to return URL of current tab of front window',
    "Google Chrome": 'tell application "Google Chrome" to return URL of active tab of front window',
    "Arc": 'tell application "Arc" to return URL of active tab of front window',
    "Brave Browser": 'tell application "Brave Browser" to return URL of active tab of front window',
    "Microsoft Edge": 'tell application "Microsoft Edge" to return URL of active tab of front window',
}

_OBSERVER_CLS = None


def _observer_class():
    """Define the ObjC observer class once per process (redefine crashes)."""
    global _OBSERVER_CLS
    if _OBSERVER_CLS is None:
        from Foundation import NSObject

        class _CoworkActivationObserver(NSObject):
            def activated_(self, note):
                cb = getattr(self, "_on_activate", None)
                if cb is None:
                    return
                app = (note.userInfo() or {}).get("NSWorkspaceApplicationKey")
                if app is None:
                    return
                try:
                    cb(str(app.localizedName() or ""), int(app.processIdentifier()))
                except Exception as e:  # never let an exception cross into ObjC
                    print(f"[cowork-window] observer cb error: {e!r}", flush=True)

        _OBSERVER_CLS = _CoworkActivationObserver
    return _OBSERVER_CLS


class _ActivationWatcher:
    """Dedicated thread: registers the NSWorkspace observer and pumps the
    runloop in bounded slices (calendar's busy-spin guard)."""

    def __init__(self, on_activate):
        self._on_activate = on_activate
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="cowork-window-runloop", daemon=True
        )

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3)

    def _run(self):
        from AppKit import NSWorkspace, NSWorkspaceDidActivateApplicationNotification
        from Foundation import NSDate, NSRunLoop

        observer = _observer_class().alloc().init()
        observer._on_activate = self._on_activate
        center = NSWorkspace.sharedWorkspace().notificationCenter()
        center.addObserver_selector_name_object_(
            observer, "activated:", NSWorkspaceDidActivateApplicationNotification, None
        )
        rl = NSRunLoop.currentRunLoop()
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                rl.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(RUNLOOP_SLICE))
                elapsed = time.monotonic() - t0
                if elapsed < RUNLOOP_SLICE:
                    self._stop.wait(RUNLOOP_SLICE - elapsed)
        finally:
            center.removeObserver_(observer)


def _idle_seconds() -> float:
    import Quartz

    state = getattr(Quartz, "kCGEventSourceStateHIDSystemState", 1)
    return round(
        Quartz.CGEventSourceSecondsSinceLastEventType(
            state, int(Quartz.kCGAnyInputEventType)
        ),
        1,
    )


def _focused_window(pid: int) -> tuple[str | None, str | None]:
    """(window_title, document_path) via AX. Best-effort — (None, None) on
    any AX error (no permission, app has no windows, etc.)."""
    from ApplicationServices import (
        AXUIElementCopyAttributeValue,
        AXUIElementCreateApplication,
        kAXDocumentAttribute,
        kAXFocusedWindowAttribute,
        kAXTitleAttribute,
    )

    try:
        app = AXUIElementCreateApplication(pid)
        err, win = AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute, None)
        if err != 0 or win is None:
            return None, None
        _, title = AXUIElementCopyAttributeValue(win, kAXTitleAttribute, None)
        _, doc = AXUIElementCopyAttributeValue(win, kAXDocumentAttribute, None)
        path = None
        if doc:
            s = str(doc)
            if s.startswith("file://"):
                from urllib.parse import unquote, urlparse

                path = unquote(urlparse(s).path)
            elif s.startswith("/"):
                path = s
        return (str(title) if title else None), path
    except Exception:
        return None, None


def _browser_url(app_name: str) -> str | None:
    script = _BROWSER_URL_SCRIPTS.get(app_name)
    if not script:
        return None
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=3,
        )
        url = out.stdout.strip()
        return url if out.returncode == 0 and url.startswith(("http", "file")) else None
    except Exception:
        return None


def _build_window_payload(app_name: str, pid: int) -> dict:
    title, doc_path = _focused_window(pid)
    payload = {
        "ts": common.now_iso(),
        "app": app_name,
        "window": title,
        "pid": pid,
        "idle_seconds": _idle_seconds(),
    }
    url = _browser_url(app_name)
    if url:
        payload["url"] = url
    elif doc_path:
        payload["file_path"] = doc_path
    return payload


def _sample_clipboard(app_name: str, last_count: int | None) -> int:
    """Compare NSPasteboard.changeCount to the last sample; log+emit on move.
    Returns the current count. First call only seeds (no retroactive log)."""
    from AppKit import NSPasteboard, NSPasteboardTypeString

    pb = NSPasteboard.generalPasteboard()
    count = int(pb.changeCount())
    if last_count is None or count == last_count:
        return count
    content = pb.stringForType_(NSPasteboardTypeString)
    if content is not None:
        text = str(content)[: common.NDJSON_TEXT_CAP]
        ctype = "string"
    else:
        text = None
        types = {str(t) for t in (pb.types() or [])}
        if "public.file-url" in types:
            ctype = "file-url"
        elif types & {"public.png", "public.tiff"}:
            ctype = "image"
        else:
            ctype = "other"
    entry = {"ts": common.now_iso(), "app": app_name, "content": text, "type": ctype}
    common.append_ndjson(CLIPBOARD_LOG, entry)
    ev_text, truncated = common.event_text(text) if text else (None, False)
    P.emit_event({
        "source": "cowork", "kind": "clipboard_changed",
        "app": app_name, "content": ev_text, "truncated": truncated,
        "type": ctype, "ts": entry["ts"],
    })
    return count


async def run() -> None:
    try:
        from ApplicationServices import AXIsProcessTrusted
        import AppKit  # noqa: F401 — fail fast if pyobjc missing
    except ImportError as e:
        print(f"[cowork-window] pyobjc missing ({e!r}); driver idle", flush=True)
        return
    if not AXIsProcessTrusted():
        print(
            "[cowork-window] Accessibility not granted for this process — "
            "grant it in System Settings > Privacy & Security > Accessibility; "
            "exiting cleanly",
            flush=True,
        )
        return
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    def on_activate(name: str, pid: int) -> None:
        try:
            loop.call_soon_threadsafe(q.put_nowait, (name, pid))
        except RuntimeError:
            pass  # loop closing

    watcher = _ActivationWatcher(on_activate)
    watcher.start()
    print("[cowork-window] watching app activations", flush=True)
    last_count: int | None = None
    try:
        while True:
            name, pid = await q.get()
            if not common.capture_enabled():
                last_count = None  # re-seed when re-enabled: no retroactive copy log
                continue
            payload = await asyncio.to_thread(_build_window_payload, name, pid)
            common.append_ndjson(WINDOW_LOG, payload)
            P.emit_event({"source": "cowork", "kind": "window_changed", **payload})
            last_count = await asyncio.to_thread(_sample_clipboard, name, last_count)
    except asyncio.CancelledError:
        print("[cowork-window] stopped", flush=True)
        raise
    finally:
        watcher.stop()
```

If the Task 3 probe showed activations only deliver on a different registration variant, adapt `_ActivationWatcher` to the variant that worked and note it in the module docstring.

- [ ] **Step 4: smoke test outside the kernel** — `PAI_ROOT=~/.pai ~/.pai/usr/lib/venv/bin/python -c` snippet that imports `drivers.cowork.window_activity` (with `~/.pai/usr/lib` on `sys.path` the way the kernel has it — easiest: `cd ~/.pai && usr/lib/venv/bin/python -c "import sys; sys.path.insert(0, 'usr/lib'); import asyncio; from drivers.cowork import window_activity as w; asyncio.run(asyncio.wait_for(w.run(), 10))"` after Task 7's install) — deferred to Task 7 verification if import scaffolding isn't in place yet; at minimum `python -m py_compile` every new file now.

- [ ] **Step 5: Commit** — `git -C ~/Projects/pairegistry add drivers/cowork && git commit -m "cowork driver: window/clipboard tracker + manifests" && git push`

---

### Task 5: cowork driver — file activity tracker (FSEvents)

**Files:**
- Create: `~/Projects/pairegistry/drivers/cowork/file_activity.py`

**Interfaces:**
- Consumes: `common.*` from Task 4; `pyobjc-framework-FSEvents`.
- Produces: emits raw kind `file_changed` (`source: cowork`); appends `file_activity.ndjson`. Slug `cowork-files` (already declared in Task 4's events.yaml).

- [ ] **Step 1: `file_activity.py`** — complete file:

```python
"""Cowork file-activity tracker: one FSEventStream over the owner's real
home, per-file events, two-layer noise suppression (FSEvents exclusion paths
for the big prefix offenders; an in-callback denylist for the scattered
patterns). Logs raw change flags — never a classified operation — and
enriches created/renamed paths with the kMDItemWhereFroms source URL.

Requires Full Disk Access for complete coverage; without it macOS silently
redacts protected subtrees (we log one warning and keep going — FDA granted
later is picked up on the next event with no code change).
"""
import asyncio
import os
import plistlib
import pwd
import threading
import time
from pathlib import Path

from boot import paths
from boot import processes as P
from drivers.cowork import common

FILE_LOG = common.STATE_DIR / "file_activity.ndjson"
RUNLOOP_SLICE = 1.0
FSEVENTS_LATENCY = 1.0  # kernel-side batching; not a poll

# Layer 1 — FSEvents exclusion paths (kernel-side, prefix-matched, max 8).
# PAI_ROOT is excluded above all: the runtime's own churn (logs, events,
# these very ndjson appends) must never feed back into the stream.
def _exclusions(home: Path) -> list[str]:
    return [str(p) for p in (
        home / "Library",
        paths.PAI_ROOT,
        home / ".cache",
        home / ".Trash",
        home / ".npm",
        home / ".cargo",
        home / ".local",
        home / ".claude",
    )]

# Layer 2 — in-callback denylist for what prefixes can't catch.
_DENY_SEGMENTS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache", "Caches",
    "DerivedData", ".Trash", ".npm", ".cargo", ".rustup", ".gradle", ".m2",
    ".docker", ".ollama", ".nvm", ".pyenv", ".vscode", ".cursor", ".pai",
}
_DENY_SUFFIXES = (
    ".tmp", ".part", ".swp", ".swx", "-wal", "-shm", "-journal",
    ".crdownload", ".download",
)
_DENY_BASENAMES = {".DS_Store", ".localized"}

_FLAG_NAMES: list[tuple[int, str]] = []  # populated in run() after import


def _denied(path: str) -> bool:
    p = Path(path)
    if p.name in _DENY_BASENAMES or p.name.endswith(_DENY_SUFFIXES):
        return True
    return bool(_DENY_SEGMENTS.intersection(p.parts))


def _change_names(flags: int) -> list[str]:
    return [name for bit, name in _FLAG_NAMES if flags & bit]


def _source_url(path: str) -> str | None:
    try:
        raw = os.getxattr(path, "com.apple.metadata:kMDItemWhereFroms")
        vals = plistlib.loads(raw)
        if isinstance(vals, list) and vals:
            return str(vals[0])
    except OSError:
        pass
    except Exception:
        pass
    return None


class _StreamWatcher:
    """Dedicated thread owning the FSEventStream + its runloop."""

    def __init__(self, root: Path, on_batch):
        self._root = root
        self._on_batch = on_batch  # callable(list[tuple[path, flags]])
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="cowork-files-runloop", daemon=True
        )
        self.failed: str | None = None

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self):
        import FSEvents
        from Foundation import NSDate, NSRunLoop

        def callback(stream, info, num, events_paths, events_flags, event_ids):
            batch = []
            for i in range(num):
                path = str(events_paths[i])
                if _denied(path):
                    continue
                batch.append((path, int(events_flags[i])))
            if batch:
                self._on_batch(batch)

        stream = FSEvents.FSEventStreamCreate(
            None, callback, None, [str(self._root)],
            FSEvents.kFSEventStreamEventIdSinceNow, FSEVENTS_LATENCY,
            FSEvents.kFSEventStreamCreateFlagFileEvents
            | FSEvents.kFSEventStreamCreateFlagUseCFTypes
            | FSEvents.kFSEventStreamCreateFlagNoDefer,
        )
        if stream is None:
            self.failed = "FSEventStreamCreate returned NULL"
            return
        FSEvents.FSEventStreamSetExclusionPaths(stream, _exclusions(self._root))
        FSEvents.FSEventStreamScheduleWithRunLoop(
            stream,
            FSEvents.CFRunLoopGetCurrent(),
            FSEvents.kCFRunLoopDefaultMode,
        )
        if not FSEvents.FSEventStreamStart(stream):
            self.failed = "FSEventStreamStart failed"
            FSEvents.FSEventStreamInvalidate(stream)
            FSEvents.FSEventStreamRelease(stream)
            return
        rl = NSRunLoop.currentRunLoop()
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                rl.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(RUNLOOP_SLICE))
                elapsed = time.monotonic() - t0
                if elapsed < RUNLOOP_SLICE:
                    self._stop.wait(RUNLOOP_SLICE - elapsed)
        finally:
            FSEvents.FSEventStreamStop(stream)
            FSEvents.FSEventStreamInvalidate(stream)
            FSEvents.FSEventStreamRelease(stream)


async def run() -> None:
    try:
        import FSEvents
    except ImportError as e:
        print(f"[cowork-files] pyobjc FSEvents missing ({e!r}); driver idle", flush=True)
        return
    global _FLAG_NAMES
    _FLAG_NAMES = [
        (int(FSEvents.kFSEventStreamEventFlagItemCreated), "created"),
        (int(FSEvents.kFSEventStreamEventFlagItemRemoved), "removed"),
        (int(FSEvents.kFSEventStreamEventFlagItemRenamed), "renamed"),
        (int(FSEvents.kFSEventStreamEventFlagItemModified), "modified"),
    ]
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)  # real home, never $HOME
    # FDA detection is indirect: probe a TCC-protected subtree we excluded
    # anyway. Unreadable => coverage is partial. One warning, keep running.
    probe = home / "Library" / "Mail"
    try:
        os.listdir(probe)
    except PermissionError:
        print(
            "[cowork-files] Full Disk Access not granted — file coverage is "
            "partial until it is (System Settings > Privacy & Security > "
            "Full Disk Access)",
            flush=True,
        )
    except OSError:
        pass
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    def on_batch(batch):
        try:
            loop.call_soon_threadsafe(q.put_nowait, batch)
        except RuntimeError:
            pass

    watcher = _StreamWatcher(home, on_batch)
    watcher.start()
    print(f"[cowork-files] watching {home} (FSEvents, per-file)", flush=True)
    try:
        while True:
            batch = await q.get()
            if watcher.failed:
                print(f"[cowork-files] stream failed: {watcher.failed}", flush=True)
                return
            if not common.capture_enabled():
                continue
            for path, flags in batch:
                change = _change_names(flags)
                if not change:
                    continue  # metadata-only churn (xattr/inode-meta/etc.)
                payload = {"ts": common.now_iso(), "path": path, "change": change}
                if "created" in change or "renamed" in change:
                    url = await asyncio.to_thread(_source_url, path)
                    if url:
                        payload["source_url"] = url
                common.append_ndjson(FILE_LOG, payload)
                P.emit_event({"source": "cowork", "kind": "file_changed", **payload})
    except asyncio.CancelledError:
        print("[cowork-files] stopped", flush=True)
        raise
    finally:
        watcher.stop()
```

- [ ] **Step 2:** `python -m py_compile` the file; quick standalone 15s live test with the venv python (touch/rm a file in `~/scratch-test/`, confirm printed/logged lines, confirm `~/Library` churn absent). Verify no feedback loop: the ndjson append itself must not appear (PAI_ROOT excluded).

- [ ] **Step 3: Commit** — `git -C ~/Projects/pairegistry add drivers/cowork && git commit -m "cowork driver: FSEvents file-activity tracker" && git push`

---

### Task 6: notetaker driver — bundle + recorder + transcription

**Files:**
- Create: `~/Projects/pairegistry/drivers/notetaker/__init__.py`, `package.yaml`, `events.yaml`, `recorder.py`, `transcribe.py`, `libexec/install.sh`

**Interfaces:**
- Consumes: Task 3's probed tap/aggregate call signatures; `drivers.voice.stt` (`WHISPER_BIN`-style paths — check `stt.py:11-13` for exact names); `drivers.voice_cloud.provider.transcribe(audio: bytes, *, content_type, filename, ...) -> str`; freeze `sys/drivers/notetaker/capture.freeze`.
- Produces: command spool `sys/drivers/notetaker/commands/*.yaml` (`{action: start|stop, cloud: bool}`, consumed+deleted); sessions under `sys/drivers/notetaker/sessions/<id>/` (`audio.raw` → transient, `transcript.json`, `status`); marker file `sys/drivers/notetaker/recording`; events `notetaker:recording_started`, `notetaker:recording_stopped`, `notetaker:transcript_ready`, `notetaker:transcript_failed`. Slug `notetaker`.

- [ ] **Step 1: manifests.** `package.yaml`:

```yaml
name: notetaker
kind: driver
version: 0.1.0
description: "Notetaker: owner-triggered local call recording (Core Audio process tap + mic) and transcription. Default-off capability; records third parties only on explicit instruction."
deps:
  - voice
hooks:
  install:
    - "bash usr/lib/drivers/notetaker/libexec/install.sh"
```

`libexec/install.sh` (voice preamble +): `uv pip install --python "$VENV_PY" pyobjc-framework-CoreAudio soundfile numpy`

`events.yaml`: process `{slug: notetaker, module: drivers.notetaker.recorder, entrypoint: run}`; document the four event kinds + the command-spool action contract in comments (the `actions` are files, not kernel events — say so explicitly).

- [ ] **Step 2: `recorder.py`.** Complete implementation outline (fill signatures from the Task 3 probe — they are recorded in its notes):

```python
"""Notetaker recorder: owner-triggered call capture + transcription.

Trigger surface: YAML command files dropped into
sys/drivers/notetaker/commands/ ({action: start|stop, cloud: bool}) watched
with a watchdog Observer (the email-drafts spool pattern) — the kernel event
bus routes to PAIs, not drivers, so owner actions arrive as files.

Capture: a Core Audio process tap (CATapDescription stereo global tap) plus
the default input device, combined in a private-ish aggregate device, read as
one input stream via sounddevice, downmixed to mono s16le and streamed to
sessions/<id>/audio.raw (raw PCM: crash-safe — a dead process leaves a valid
stream; finalize converts to 16k WAV via ffmpeg).

Requires the System Audio Recording permission (Screen Recording pane).
AudioHardwareCreateProcessTap returning nonzero => refuse start with a clear
message; never a silent no-op.
"""
```

Key functions (write fully during execution, guided by the probe):
- `_watch_commands(loop, q)` — watchdog `Observer` on `COMMANDS_DIR`, on-created → `loop.call_soon_threadsafe(q.put_nowait, path)`; also scan pre-existing files at boot.
- `_start_session(cloud: bool) -> Session | None` — refuse if `FREEZE_PATH.exists()` (capability off), if already recording, or if tap creation fails (print the grant walk-through). Create tap + aggregate, `sd._terminate(); sd._initialize()`, locate aggregate by name, open `sd.InputStream(device=idx, dtype="int16", callback=...)` writing downmixed mono frames to `audio.raw`. Write `recording` marker with session id; write session `status` = `recording`; emit `recording_started`.
- `_stop_session(s) -> None` — close stream; destroy aggregate + tap; remove marker; emit `recording_stopped`; then transcribe in `asyncio.to_thread` (below); auto-stop convenience is NOT implemented in v1 beyond process cancellation (manual stop is authoritative — matches spec's "manual stop is authoritative"; the call-app-quit auto-stop is deferred).
- Boot recovery: any session dir with `status == recording` at startup → mark `interrupted`, attempt transcription of the partial `audio.raw`.
- `run()` — freeze-gated command loop: `start`/`stop` commands pulled from the queue; unknown/duplicate commands logged and ignored; command files deleted after processing.

- [ ] **Step 3: `transcribe.py`** — `transcribe_session(session_dir: Path, cloud: bool) -> dict`:
  - Finalize: `ffmpeg -f s16le -ar <rate> -ac 1 -i audio.raw -ar 16000 -ac 1 audio16.wav` (rate stored in a `meta.yaml` written at start).
  - Local: run the whisper binary from `drivers.voice.stt`'s module-level paths with `-oj` (JSON output), parse `transcription[]` → `segments: [{start, end, text}]` (offsets are ms → seconds). Fall back to `stt.transcribe()` plain text as one segment if `-oj` parsing fails.
  - Cloud: `ffmpeg → .m4a 64k`; if > 24 MB split `-f segment -segment_time 600`; per chunk `voice_cloud.provider.transcribe(bytes, content_type="audio/mp4", filename="chunk.m4a")`; one segment per chunk with boundary timestamps.
  - Write `transcript.json` `{session_id, started, ended, cloud, segments}`; on success delete `audio.raw`/`audio16.wav`, status `done`, emit `transcript_ready` `{session_id, transcript_path (FHS-relative), cloud}`; on failure keep audio, status `failed`, emit `transcript_failed` `{session_id, error}`.

- [ ] **Step 4:** `py_compile` all files; standalone probe-driven smoke: start a session from a scratch script (not the kernel), play audio + speak, stop, confirm `transcript.json` has plausible text and `audio.raw` was deleted.

- [ ] **Step 5: Commit** — `git -C ~/Projects/pairegistry add drivers/notetaker && git commit -m "notetaker driver: local call recording + transcription" && git push`

---

### Task 7: web console recording indicator

**Files:**
- Modify: `~/Projects/pai/src/usr/libexec/web/pai_web/hub.py` (watch pattern examples at :412-:418, :505-:511)
- Modify: `~/Projects/pai/src/usr/libexec/web/src/types.ts`, `App.tsx`, `components/StatusBar.tsx` (or wherever the status bar lives)

**Interfaces:**
- Consumes: marker file `sys/drivers/notetaker/recording` (Task 6).
- Produces: WS message `{"type": "notetaker_recording", "recording": bool}` + `hello.notetaker_recording`; a red "● recording" pill in the status bar while present.

- [ ] **Step 1:** hub: add a `_Debounced` watcher on `sys/drivers/notetaker/` (mirror the etc-watch at hub.py:412) broadcasting marker presence on change, plus initial state in `hello`.
- [ ] **Step 2:** frontend: type + state + pill (`recording` red dot, right side of status bar). `pnpm build`.
- [ ] **Step 3:** Commit — `git -C ~/Projects/pai add -A && git commit -m "web: notetaker recording indicator" && git push`

---

### Task 8: Rollout to the live runtime

**Files:**
- Modify: `~/Projects/pai/pyproject.toml` (dependencies list, :20-:22 region)
- Live runtime state only otherwise.

- [ ] **Step 1: dev-parity deps** — `cd ~/Projects/pai && uv add pyobjc-framework-ApplicationServices pyobjc-framework-FSEvents pyobjc-framework-CoreAudio` (keeps repo-run kernels + tests importing what the drivers import); `uv run python -m pytest` → green. Commit + push.
- [ ] **Step 2: install driver bundles** — `paiman install cowork && paiman install notetaker` (installs registry bundles + runs install hooks into `~/.pai`). Verify `~/.pai/usr/lib/drivers/cowork/events.yaml` exists and the venv imports `ApplicationServices`, `FSEvents`, `CoreAudio`.
- [ ] **Step 3: release + deploy kernel** — `uv run pairelease --publish`, then `pai update`, then `~/.pai/sbin/reboot`. Watch `~/.pai/var/log/kernel/kernel.log` for the re-exec banner and `[cowork-window] watching app activations` / `[cowork-files] watching ...` lines.
- [ ] **Step 4: cowork manual verification (spec's checklist)** — switch apps → `sys/drivers/cowork/window_activity.ndjson` gains lines (browser line has `url`, Preview/VSCode line has `file_path`, `idle_seconds` sane); copy text + switch → `clipboard.ndjson` line + event; download a file + `mv`/`rm` in `~/some-dir` → `file_activity.ndjson` lines (`source_url` on the download); confirm log stays human-paced (no Library/cache flood); web console → toggle Cowork Mode off → no new lines/events; back on → resumes. Confirm PAI received `cowork:*` events (kernel.log routing lines).
- [ ] **Step 5: notetaker manual verification** — enable Notetaker in console (writes `capabilities.notetaker: yes`); grant the System Audio Recording TCC when prompted; play audio + speak, drop a start command (ask PAI or write the YAML by hand), confirm console shows the recording pill + `recording_started` event; stop; confirm `transcript.json`, audio deleted, `transcript_ready` event, PAI writes a summary under its home `notes/calls/`. Repeat with `cloud: true`. Then set capability back to `no` and confirm start refuses.
- [ ] **Step 6: docs + memory** — flip both specs' Status lines to `Implemented (2026-07-07)`; update the `project_cowork_mode` memory (built + live, where things landed); note FDA remains owner-granted (surface in final report if not yet granted).
- [ ] **Step 7: final commit/push both repos.**

---

## Self-Review notes

- Spec coverage: window (Task 4), clipboard piggyback (Task 4), file activity + denylist + FDA warning + whereFroms (Task 5), capability flag default-yes + toggle + live reconcile via freeze projection (Tasks 1-2), prompt disclosure (Task 2), notetaker two-tier consent + default-no + manual trigger + local/cloud STT + audio deletion + failure retention + crash recovery + visible recording (Tasks 1, 2, 6, 7). Deferred items from both specs stay deferred.
- The spec's "driver reconciles live, no restart" is satisfied by the freeze file: web toggle → `set_capability_mode` → `kernel:reload_config` → `reconcile_from_config` → `project_capabilities` → freeze appears/disappears → per-event `capture_enabled()` check reacts on the next event.
- Notetaker's in-console "recording" indicator is Task 7; PAI's chat announcements ride the `recording_started/stopped` events plus the prompt-block instruction (Task 2).
- Type consistency: `capture_enabled()`/`append_ndjson`/`now_iso`/`event_text` defined once in `common.py`, consumed by both trackers; freeze filename `capture.freeze` used identically in Task 1 specs and both drivers.
