"""Textual app: PAI operator console."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

_PAI_CMD = re.compile(r"^\[pai(?::[^\]]+)?\] \$ ")
_PAI_REPLY = re.compile(r"^\[pai(?::[^\]]+)?\] ")

import yaml
from functools import partial

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal, Vertical
from textual.widgets import Header, Input, Static

from kernel.processes import LIVE_DIR, emit_event

PROVIDER_CONFIG_PATH = LIVE_DIR / "memory" / "myself" / "provider.yaml"
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
    ]
    COMMANDS = App.COMMANDS | {ProviderCommands}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="chat-col"):
                yield ChatPane(id="chat", wrap=True, markup=False)
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
        self._me = MeThreadWatcher(loop)
        self._procs = ProcWatcher(loop)
        self._events = EventsWatcher(loop)
        self._log = LogTailer(loop)

        self._me.start()
        self._procs.start()
        self._events.start()
        self._log.start()

        self._tasks = [
            asyncio.create_task(self._pump_me(), name="pump-me"),
            asyncio.create_task(self._pump_procs(), name="pump-procs"),
            asyncio.create_task(self._pump_events(), name="pump-events"),
            asyncio.create_task(self._pump_log(), name="pump-log"),
        ]
        self.query_one("#input", Input).focus()

    async def on_unmount(self) -> None:
        for t in getattr(self, "_tasks", []):
            t.cancel()
        for w in (getattr(self, "_me", None), getattr(self, "_procs", None),
                  getattr(self, "_events", None), getattr(self, "_log", None)):
            if w is not None:
                w.stop()

    # --- pumps -----------------------------------------------------------

    async def _pump_me(self) -> None:
        chat = self.query_one("#chat", ChatPane)
        while True:
            snap = await self._me.next()
            chat.render_snapshot(snap)
            chat.scroll_end(animate=False)

    async def _pump_procs(self) -> None:
        procs = self.query_one("#procs", ProcList)
        while True:
            rows = await self._procs.next()
            procs.render_rows(rows)

    async def _pump_events(self) -> None:
        strip = self.query_one("#events", EventStrip)
        while True:
            sight = await self._events.next()
            strip.write_sighting(sight)

    async def _pump_log(self) -> None:
        tail = self.query_one("#log", LogTail)
        activity = self.query_one("#activity", PaiActivity)
        status = self.query_one("#status", Static)
        while True:
            line = await self._log.next()
            tail.write_line(line)
            activity.ingest(line)

            if "nudge failed" in line or "nudge complete" in line:
                status.update("idle")
            elif line.startswith("[kernel] nudge:"):
                status.update("PAI is thinking…")
            elif _PAI_CMD.match(line):
                # Still working — commands keep arriving.
                status.update("PAI is thinking…")
            elif _PAI_REPLY.match(line):
                # PAI's final text reply.
                status.update("idle")

    # --- input handler ---------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""

        # 1. Append to today's me/ day-file.
        path = today_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = f"[{datetime.now().strftime('%H:%M')}] me: {text}\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(line)

        # 2. Wake the kernel via an event file.
        emit_event({
            "source": "tui",
            "kind": "new_message",
            "thread": "me",
            "text": text,
        })
        self.query_one("#status", Static).update("sent → waiting for kernel…")

    def set_provider(self, key: str) -> None:
        _write_provider(key)

    async def action_interrupt(self) -> None:
        emit_event({"source": "tui", "kind": "interrupt", "pai": 1})
        self.query_one("#status", Static).update("interrupt sent → cancelling…")
