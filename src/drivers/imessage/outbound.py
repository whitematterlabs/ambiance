"""iMessage outbound driver.

Tails threads with `channel: imessage` in their meta.yaml. PAI signals a
send by appending a *bare* line (no `[HH:MM] sender:` prefix) to a
day-file: we send it via Messages.app (osascript), then append the
canonical `[HH:MM] me: <text>` record to the same file. That canonical
line is one-shot suppressed on the tailer so we don't re-read it.

Bracketed lines (`[HH:MM] sender: ...`) are *log entries only* — never
sends. This includes `me:` lines the kernel writes when chat.db echoes
back a message Arda sent from his phone: those lines are the record of
the send, not a request to re-send.

New thread dirs — when `communication/messages/{slug}/` appears with no
meta.yaml, we materialize one from `memory/people/{slug}/about.yaml` (or,
for a raw phone/email slug, from the slug itself). PAI's workflow
collapses to `mkdir messages/{slug} && echo "text" >> messages/{slug}/$(date +%F).md`.

Tries iMessage first, falls back to SMS if iMessage errors (covers
Android contacts when "Send as SMS" isn't doing the fallback itself).
SMS fallback requires Text Message Forwarding from your iPhone.

Permanent failures (both services error) are surfaced to PAI via a
`kernel: send failed` note in the thread day-file and a `send_failed`
event. The tailer cursor advances on failure so we don't retry forever.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from boot import outbound_echo
from boot import processes as P

from boot import paths

from ..tailer import Tailer

# Watch the canonical spool directly. v3: messages live at
# /var/spool/communication/ and are shared across the fleet (each PAI's
# /home/<pai>/communication/ is just a symlink view); the outbound
# driver is system-shared, not per-PAI.
MESSAGES_ROOT = paths.var_spool_messages()
PEOPLE_ROOT = paths.var_lib_memory() / "people"

# Bracketed prefix — log entries (inbound, canonical me:, kernel notes).
# Never treated as send requests; only bare lines are.
BRACKET_LINE = re.compile(r"^\[")
# Phone slug = all digits (after earlier `h`-prefix removal); email slug
# contains `@` (unusual but handled).
_PHONE_SLUG = re.compile(r"^\d{7,}$")


def _load_meta(day_file: Path) -> Optional[dict]:
    meta_path = day_file.parent / "meta.yaml"
    if not meta_path.exists():
        return None
    try:
        with meta_path.open() as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError:
        return None


def _owned(path: Path) -> bool:
    if path.suffix != ".md":
        return False
    if path.parent.parent != MESSAGES_ROOT.resolve() and path.parent.parent != MESSAGES_ROOT:
        return False
    meta = _load_meta(path)
    if not meta:
        return False
    return meta.get("channel") == "imessage"


def _applescript_for_1to1(handle: str, text: str, service_type: str) -> str:
    # Both strings are interpolated into AppleScript double-quoted strings.
    h = handle.replace("\\", "\\\\").replace('"', '\\"')
    t = text.replace("\\", "\\\\").replace('"', '\\"')
    return (
        'tell application "Messages"\n'
        f'  set targetService to 1st service whose service type = {service_type}\n'
        f'  set targetBuddy to buddy "{h}" of targetService\n'
        f'  send "{t}" to targetBuddy\n'
        'end tell'
    )


def _applescript_for_group(chat_guid: str, text: str) -> str:
    g = chat_guid.replace("\\", "\\\\").replace('"', '\\"')
    t = text.replace("\\", "\\\\").replace('"', '\\"')
    return (
        'tell application "Messages"\n'
        f'  set targetChat to chat id "{g}"\n'
        f'  send "{t}" to targetChat\n'
        'end tell'
    )


async def _run_osascript(script: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    return (
        proc.returncode if proc.returncode is not None else -1,
        stderr.decode("utf-8", errors="replace").strip(),
    )


def _append_kernel_note(day_file: Path, note: str) -> None:
    hm = datetime.now().strftime("%H:%M")
    with day_file.open("a", encoding="utf-8") as f:
        f.write(f"[{hm}] kernel: {note}\n")


async def _send(meta: dict, text: str) -> str:
    """Send one line; return the service used. Raises on permanent failure."""
    if meta.get("group"):
        chat_guid = meta.get("chat_guid")
        if not chat_guid:
            raise RuntimeError("group thread missing chat_guid")
        script = _applescript_for_group(chat_guid, text)
        code, err = await _run_osascript(script)
        if code != 0:
            raise RuntimeError(f"group send failed: exit {code} — {err}")
        return "iMessage"

    handles = meta.get("handles") or []
    if not handles:
        raise RuntimeError("1:1 thread missing handles")

    errors: list[str] = []
    for service in ("iMessage", "SMS"):
        script = _applescript_for_1to1(handles[0], text, service)
        code, err = await _run_osascript(script)
        if code == 0:
            return service
        errors.append(f"{service}: exit {code} — {err}")
    raise RuntimeError(" | ".join(errors))


def _emit_send_failed(thread: str, text: str, reason: str) -> None:
    P.emit_event({
        "source": "imessage-out",
        "kind": "send_failed",
        "thread": thread,
        "text": text,
        "reason": reason,
    })


def _append_canonical(day_file: Path, text: str) -> str:
    """Append `[HH:MM] me: <text>` to the day-file and return the exact
    line string (for suppression registration)."""
    hm = datetime.now().strftime("%H:%M")
    line = f"[{hm}] me: {text}"
    with day_file.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return line


async def _process_send(path: Path, text: str) -> bool:
    """Send `text` out through the meta for `path`'s thread. Returns True
    on success, False on permanent failure (note + event already emitted)."""
    meta = _load_meta(path)
    if not meta or meta.get("channel") != "imessage":
        return False
    thread = path.parent.name
    try:
        service = await _send(meta, text)
    except Exception as e:
        reason = str(e)
        print(f"[imessage-out] send failed to {thread}: {reason}", flush=True)
        try:
            _append_kernel_note(path, f"send failed — {reason}")
        except Exception as note_err:
            print(f"[imessage-out] could not append kernel note: {note_err}", flush=True)
        _emit_send_failed(thread, text, reason)
        return False
    print(f"[imessage-out] sent to {thread} via {service}: {text[:80]}", flush=True)
    return True


def _materialize_meta(thread_dir: Path) -> bool:
    """Create meta.yaml for a new thread dir from memory/people or the slug
    itself. Returns True if meta.yaml now exists (either created or already
    present), False if we have no way to populate it."""
    meta_path = thread_dir / "meta.yaml"
    if meta_path.exists():
        return True
    slug = thread_dir.name

    # Source 1: memory/people/{slug}/about.yaml — the address-book path.
    person_about = PEOPLE_ROOT / slug / "about.yaml"
    handles: list[str] = []
    display_name: Optional[str] = None
    if person_about.exists():
        try:
            with person_about.open() as f:
                data = yaml.safe_load(f) or {}
            handles = [str(h) for h in (data.get("handles") or []) if h]
            display_name = data.get("name") or None
        except yaml.YAMLError:
            pass

    # Source 2: slug IS the handle (raw phone digits or an email).
    if not handles:
        if _PHONE_SLUG.match(slug):
            handles = [f"+{slug}"]
        elif "@" in slug:
            handles = [slug.lower()]

    if not handles:
        print(
            f"[imessage-out] new thread {slug}/ has no matching people entry "
            f"and no handle-like slug; leaving meta.yaml empty",
            flush=True,
        )
        return False

    meta: dict = {
        "description": "",
        "created": datetime.now().date().isoformat(),
        "group": False,
        "handles": handles,
        "channel": "imessage",
    }
    if display_name:
        meta["display_name"] = display_name
    with meta_path.open("w") as f:
        yaml.safe_dump(meta, f, sort_keys=False)

    # Link person into thread if present, matching messages._create_person.
    person_dir = PEOPLE_ROOT / slug
    link = thread_dir / slug
    if person_dir.is_dir() and not link.exists():
        link.symlink_to(Path("..") / ".." / ".." / "memory" / "people" / slug)

    print(f"[imessage-out] materialized meta.yaml for {slug}/ (handles={handles})", flush=True)
    return True


def build() -> Tailer:
    tailer: Tailer

    async def _on_dir_created(path: Path) -> None:
        # Only top-level thread dirs: messages/{slug}/. Ignore deeper paths.
        try:
            path = path.resolve()
        except OSError:
            return
        if path.parent != MESSAGES_ROOT.resolve():
            return
        if not path.is_dir():
            return
        if not _materialize_meta(path):
            return
        # Drain any .md files already sitting in the new dir — PAI may have
        # done `mkdir && echo` back-to-back, and the file-event could have
        # raced the dir-event to a not-yet-owned skip.
        for child in path.iterdir():
            if child.is_file() and child.suffix == ".md":
                await tailer._drain_file(child)  # noqa: SLF001

    async def on_line(path: Path, line: str) -> None:
        if BRACKET_LINE.match(line):
            return  # log entry — inbound, canonical me:, kernel note, etc.
        text = line.rstrip()
        if not text:
            return
        # Bare line = outbound draft. Send, then write the canonical record
        # and suppress the tailer's next read of that exact line.
        ok = await _process_send(path, text)
        if not ok:
            return
        canonical = _append_canonical(path, text)
        tailer.suppress_next(path, canonical)
        outbound_echo.register(path.parent.name, text)

    tailer = Tailer(
        name="imessage-out",
        roots=[MESSAGES_ROOT],
        owned=_owned,
        on_line=on_line,
        on_dir_created=_on_dir_created,
    )
    return tailer


async def run() -> None:
    await build().run()
