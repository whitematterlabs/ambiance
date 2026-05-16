"""Textual app: PAI operator console."""

from __future__ import annotations

import asyncio
import os
import shlex
import time
from datetime import datetime
from typing import Optional

from rich.text import Text

from boot.paths import PAI_ROOT

import yaml
from functools import partial

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal, Vertical
from textual.widgets import Header, Input, Static, TabbedContent, TabPane

from boot.nudge import apply_pending_history_action
from boot.processes import HOME_DIR, emit_event, _iter_pai_specs, read_status
from boot import stitch

def _format_busy(slug: str, busy: Optional[tuple[str, float]]) -> str:
    if busy is None:
        return "idle"
    reason, started_at = busy
    reason = reason.strip() or "thinking"
    if started_at > 0:
        elapsed = max(0, int(time.time() - started_at))
        return f"{slug}: {reason} ({elapsed}s)"
    return f"{slug}: {reason}"


PROVIDER_CONFIG_PATH = HOME_DIR / "memory" / "myself" / "provider.yaml"
PROVIDER_OPTIONS = [("Anthropic", "anthropic"), ("Deepseek", "deepseek")]


def _read_provider() -> str:
    try:
        data = yaml.safe_load(PROVIDER_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return "anthropic"
    key = data.get("provider") if isinstance(data, dict) else None
    return key if key in {k for _, k in PROVIDER_OPTIONS} else "anthropic"


def _write_provider(key: str) -> None:
    PROVIDER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROVIDER_CONFIG_PATH.write_text(f"provider: {key}\n", encoding="utf-8")


class ProviderCommands(Provider):
    """Command-palette entries to swap the LLM provider."""

    def _help(self, key: str) -> str:
        return "active" if key == _read_provider() else "switch on next turn"

    async def discover(self) -> Hits:
        for label, key in PROVIDER_OPTIONS:
            yield DiscoveryHit(
                f"Provider: {label}",
                partial(self.app.set_provider, key),
                help=self._help(key),
            )

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for label, key in PROVIDER_OPTIONS:
            command = f"Provider: {label}"
            score = matcher.match(command)
            if score <= 0:
                continue
            yield Hit(
                score,
                matcher.highlight(command),
                partial(self.app.set_provider, key),
                help=self._help(key),
            )


from .state import (
    EventsWatcher,
    LogTailer,
    MeThreadWatcher,
    ProcWatcher,
    today_file,
)
from .widgets import ChatPane, EventStrip, LogTail, PaiActivity, ProcList


class TuiApp(App):
    CSS = """
    Screen {
        layers: base;
    }
    #main {
        height: 1fr;
    }
    #chat-col {
        width: 2fr;
        border-right: solid $panel;
    }
    #side-col {
        width: 1fr;
    }
    ChatPane {
        height: 1fr;
    }
    #tabs {
        height: 1fr;
    }
    #side-label-procs, #side-label-events, #side-label-log {
        background: $panel;
        color: $text-muted;
        padding: 0 1;
    }
    #input {
        height: 3;
        border: tall $accent;
    }
    #status {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
    }
    """

    TITLE = "PAI — operator console"
    BINDINGS = [
        ("ctrl+c", "quit", "quit"),
        Binding("escape", "interrupt", "interrupt PAI", priority=True),
        Binding("ctrl+tab", "next_tab", "next tab", priority=True),
        Binding("ctrl+shift+tab", "prev_tab", "prev tab", priority=True),
        *[
            Binding(f"ctrl+{n}", f"select_tab({n})", f"tab {n}", show=False, priority=True)
            for n in range(1, 10)
        ],
    ]
    COMMANDS = App.COMMANDS | {ProviderCommands}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="chat-col"):
                yield TabbedContent(id="tabs")
            with Vertical(id="side-col"):
                yield Static("running procs", id="side-label-procs")
                yield ProcList(id="procs")
                yield Static("PAI activity", id="side-label-activity")
                yield PaiActivity(id="activity", wrap=True, markup=False)
                yield Static("events (live)", id="side-label-events")
                yield EventStrip(id="events", wrap=False, markup=False)
                yield Static("kernel.log", id="side-label-log")
                yield LogTail(id="log", wrap=True, markup=False)
        yield Static("idle", id="status")
        yield Input(placeholder="message PAI... (Enter to send)", id="input")

    async def on_mount(self) -> None:
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._procs = ProcWatcher(loop)
        self._events = EventsWatcher(loop)
        self._log = LogTailer(loop)

        # Per-PAI: pid -> (watcher, pump_task). Tabs reconcile from this.
        self._pai_watchers: dict[int, MeThreadWatcher] = {}
        self._pai_pumps: dict[int, asyncio.Task] = {}

        # Discover initial fleet from home/proc/.
        for pid in self._discover_pai_pids():
            await self._add_pai_tab(pid)

        # Default to the fallback PAI (owner-facing), not the lowest pid
        # which by reservation is kernel_manager.
        fallback_pid = self._fallback_pid()
        if fallback_pid is not None and fallback_pid in self._pai_watchers:
            self.query_one("#tabs", TabbedContent).active = f"tab-{fallback_pid}"

        self._procs.start()
        self._events.start()
        self._log.start()

        self._tasks = [
            asyncio.create_task(self._pump_procs(), name="pump-procs"),
            asyncio.create_task(self._pump_events(), name="pump-events"),
            asyncio.create_task(self._pump_log(), name="pump-log"),
        ]
        self.query_one("#input", Input).focus()

    async def on_unmount(self) -> None:
        for t in getattr(self, "_tasks", []):
            t.cancel()
        for t in getattr(self, "_pai_pumps", {}).values():
            t.cancel()
        for w in getattr(self, "_pai_watchers", {}).values():
            w.stop()
        for w in (getattr(self, "_procs", None),
                  getattr(self, "_events", None), getattr(self, "_log", None)):
            if w is not None:
                w.stop()

    # --- fleet/tabs ------------------------------------------------------

    def _discover_pai_pids(self) -> list[int]:
        # Only running PAI procs get tabs. Resolved subagents leave their
        # spec on disk forever; including them would leak tabs + watchers.
        pids: list[int] = []
        for slug, spec in _iter_pai_specs():
            pid = spec.get("pid")
            if not isinstance(pid, int):
                continue
            try:
                if read_status(slug) != "running":
                    continue
            except FileNotFoundError:
                continue
            pids.append(pid)
        return sorted(pids)

    def _fallback_pid(self) -> int | None:
        for _slug, spec in _iter_pai_specs():
            if spec.get("fallback") is True:
                pid = spec.get("pid")
                if isinstance(pid, int):
                    return pid
        return None

    def _slug_for_pid(self, pid: int) -> str:
        for slug, spec in _iter_pai_specs():
            if spec.get("pid") == pid:
                return slug
        return str(pid)

    async def _add_pai_tab(self, pid: int) -> None:
        if pid in self._pai_watchers:
            return
        slug = self._slug_for_pid(pid)
        title = f"{slug} #{pid}"
        chat = ChatPane(pid=pid, id=f"chat-{pid}", wrap=True, markup=False)
        tabs = self.query_one("#tabs", TabbedContent)
        await tabs.add_pane(TabPane(title, chat, id=f"tab-{pid}"))

        watcher = MeThreadWatcher(self._loop, pid)
        watcher.start()
        self._pai_watchers[pid] = watcher
        self._pai_pumps[pid] = asyncio.create_task(
            self._pump_me(pid), name=f"pump-me-{pid}"
        )

    async def _remove_pai_tab(self, pid: int) -> None:
        watcher = self._pai_watchers.pop(pid, None)
        if watcher is not None:
            watcher.stop()
        pump = self._pai_pumps.pop(pid, None)
        if pump is not None:
            pump.cancel()
        tabs = self.query_one("#tabs", TabbedContent)
        try:
            await tabs.remove_pane(f"tab-{pid}")
        except Exception:
            pass

    def _active_pid(self) -> int | None:
        tabs = self.query_one("#tabs", TabbedContent)
        active = tabs.active or ""
        if active.startswith("tab-"):
            try:
                return int(active[len("tab-"):])
            except ValueError:
                return None
        return None

    def _ordered_pids(self) -> list[int]:
        return sorted(self._pai_watchers.keys())

    async def action_next_tab(self) -> None:
        pids = self._ordered_pids()
        if not pids:
            return
        cur = self._active_pid()
        idx = pids.index(cur) if cur in pids else -1
        nxt = pids[(idx + 1) % len(pids)]
        self.query_one("#tabs", TabbedContent).active = f"tab-{nxt}"

    async def action_prev_tab(self) -> None:
        pids = self._ordered_pids()
        if not pids:
            return
        cur = self._active_pid()
        idx = pids.index(cur) if cur in pids else 0
        prev = pids[(idx - 1) % len(pids)]
        self.query_one("#tabs", TabbedContent).active = f"tab-{prev}"

    async def action_select_tab(self, n: int) -> None:
        pids = self._ordered_pids()
        if 1 <= n <= len(pids):
            self.query_one("#tabs", TabbedContent).active = f"tab-{pids[n - 1]}"

    async def on_tabbed_content_tab_activated(self) -> None:
        # Active tab changed — re-derive status from the latest proc rows
        # so we don't wait for the next /proc/ poke.
        self._procs.queue.put_nowait(True)

    # --- pumps -----------------------------------------------------------

    async def _pump_me(self, pid: int) -> None:
        watcher = self._pai_watchers[pid]
        chat_id = f"#chat-{pid}"
        while True:
            snap = await watcher.next()
            try:
                chat = self.query_one(chat_id, ChatPane)
            except Exception:
                return
            chat.render_snapshot(snap)
            chat.scroll_end(animate=False)

    async def _pump_procs(self) -> None:
        procs = self.query_one("#procs", ProcList)
        status = self.query_one("#status", Static)
        while True:
            rows = await self._procs.next()
            procs.render_rows(rows)
            # Reconcile tabs against the running PAI fleet.
            current = set(self._discover_pai_pids())
            existing = set(self._pai_watchers.keys())
            for pid in sorted(current - existing):
                await self._add_pai_tab(pid)
            for pid in sorted(existing - current):
                await self._remove_pai_tab(pid)
            # Status bar: presence of /proc/<slug>/busy is the truth.
            self._refresh_status(rows, status)

    def _refresh_status(self, rows: list, status: Static) -> None:
        active_pid = self._active_pid()
        if active_pid is None:
            status.update("idle")
            return
        for r in rows:
            if r.pid == str(active_pid):
                status.update(_format_busy(r.slug, r.busy))
                return
        status.update("idle")

    async def _pump_events(self) -> None:
        strip = self.query_one("#events", EventStrip)
        while True:
            sight = await self._events.next()
            strip.write_sighting(sight)

    async def _pump_log(self) -> None:
        # Status bar is driven by /proc/<slug>/busy via _pump_procs, not
        # by log-line classification. Here we just tail and decorate.
        tail = self.query_one("#log", LogTail)
        activity = self.query_one("#activity", PaiActivity)
        while True:
            line = await self._log.next()
            tail.write_line(line)
            activity.ingest(line)

    # --- input handler ---------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        pid = self._active_pid()
        if pid is None:
            self.query_one("#status", Static).update("no PAI tab active")
            return

        if text.startswith("!"):
            await self._run_shell(text[1:].strip(), pid)
            return

        # 1. Append to today's me/{pid}/ day-file.
        path = today_file(pid)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = f"[{datetime.now().strftime('%H:%M')}] me: {text}\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(line)

        # 2. Wake the kernel via an event file, targeting the active PAI.
        emit_event({
            "source": "tui",
            "kind": "new_message",
            "thread": "me",
            "target_pid": pid,
            "text": text,
        })
        self.query_one("#status", Static).update(
            f"sent → pid {pid}, waiting for kernel…"
        )

    async def _run_shell(self, cmd: str, pid: int) -> None:
        """Run a shell command (from `!cmd` input) with PAI's PATH and context."""
        chat = self.query_one(f"#chat-{pid}", ChatPane)
        status = self.query_one("#status", Static)
        if not cmd:
            status.update("shell: empty command")
            return

        slug = self._slug_for_pid(pid)
        env = os.environ.copy()
        pai_path = f"{PAI_ROOT/'bin'}:{PAI_ROOT/'usr'/'bin'}"
        env["PATH"] = f"{pai_path}:{env.get('PATH', '')}"
        env["PAI_SLUG"] = slug
        env["PAI_ROOT"] = str(PAI_ROOT)

        chat.write(Text(f"$ {cmd}", style="bold yellow"))
        status.update(f"shell: running {shlex.split(cmd)[0]}…")

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(stitch.home_for(slug)),
                env=env,
            )
            out, _ = await proc.communicate()
            rc = proc.returncode or 0
        except Exception as e:
            chat.write(Text(f"shell: {e}", style="red"))
            status.update("shell: error")
            return

        text = out.decode(errors="replace").rstrip()
        if text:
            for line in text.splitlines():
                chat.write(Text(line, style="dim" if rc == 0 else "red"))
        status.update(f"shell: exit {rc}")

        # Apply any queued clear/compact action written by bin/clear or bin/compact.
        if rc == 0 and apply_pending_history_action(slug):
            chat.write(Text("context action applied.", style="dim green"))
            self.query_one("#procs", ProcList).zero_ctx(slug)

    def set_provider(self, key: str) -> None:
        _write_provider(key)

    async def action_interrupt(self) -> None:
        pid = self._active_pid() or 1
        emit_event({"source": "tui", "kind": "interrupt", "pai": pid})
        self.query_one("#status", Static).update(
            f"interrupt sent → pid {pid}, cancelled"
        )
