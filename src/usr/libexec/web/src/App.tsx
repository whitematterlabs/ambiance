import { useCallback, useEffect, useRef, useState } from "react";
import type {
  EventSighting,
  FleetMember,
  ProcRow,
  ServerMessage,
  ShellEntry,
  ThreadMessage,
} from "./types";
import { ActivityEntry, ActivityState, ingest, initialActivity } from "./activity";
import * as api from "./api";
import { Header } from "./components/Header";
import { FleetTabs } from "./components/FleetTabs";
import { ChatPane } from "./components/ChatPane";
import { StatusBar } from "./components/StatusBar";
import { MessageInput } from "./components/MessageInput";
import { ProcList } from "./components/ProcList";
import { ActivityPane } from "./components/ActivityPane";
import { EventStrip } from "./components/EventStrip";
import { LogTail } from "./components/LogTail";
import { CommandPalette } from "./components/CommandPalette";

const CAP = 500; // ring-buffer cap for log/activity/events

function cap<T>(arr: T[], extra: T[]): T[] {
  const next = arr.concat(extra);
  return next.length > CAP ? next.slice(next.length - CAP) : next;
}

export function App() {
  const [connected, setConnected] = useState(false);
  const [provider, setProvider] = useState("anthropic");
  const [fleet, setFleet] = useState<FleetMember[]>([]);
  const [activePid, setActivePid] = useState<number | null>(null);
  const [procs, setProcs] = useState<ProcRow[]>([]);
  const [threads, setThreads] = useState<Record<number, ThreadMessage[]>>({});
  const [shell, setShell] = useState<Record<number, ShellEntry[]>>({});
  const [events, setEvents] = useState<EventSighting[]>([]);
  const [logLines, setLogLines] = useState<string[]>([]);
  const [activity, setActivity] = useState<ActivityEntry[]>([]);
  const [status, setStatus] = useState<string>("idle");
  const [paletteOpen, setPaletteOpen] = useState(false);

  const activityState = useRef<ActivityState>(initialActivity());
  const activePidRef = useRef<number | null>(null);
  const fleetRef = useRef<FleetMember[]>([]);
  const procsRef = useRef<ProcRow[]>([]);
  activePidRef.current = activePid;
  fleetRef.current = fleet;
  procsRef.current = procs;

  // Refresh the status line when the active tab changes (TUI pokes /proc).
  useEffect(() => {
    setStatus(deriveStatus(procsRef.current, activePid));
  }, [activePid]);

  // Pick the fallback PAI when no tab is active (matches the TUI default).
  const ensureActive = useCallback((f: FleetMember[]) => {
    setActivePid((cur) => {
      if (cur !== null && f.some((m) => m.pid === cur)) return cur;
      const fb = f.find((m) => m.fallback);
      return fb ? fb.pid : f.length ? f[0].pid : null;
    });
  }, []);

  // --- SSE stream (kernel → browser) ---
  useEffect(() => {
    const es = new EventSource("/api/stream");
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.onmessage = (e) => {
      const msg: ServerMessage = JSON.parse(e.data);
      switch (msg.type) {
        case "hello": {
          setProvider(msg.provider);
          setFleet(msg.fleet);
          setProcs(msg.procs);
          const t: Record<number, ThreadMessage[]> = {};
          for (const [pid, m] of Object.entries(msg.threads)) t[Number(pid)] = m;
          setThreads(t);
          ensureActive(msg.fleet);
          break;
        }
        case "procs":
          setProcs(msg.rows);
          setStatus(deriveStatus(msg.rows, activePidRef.current));
          break;
        case "fleet":
          setFleet(msg.fleet);
          ensureActive(msg.fleet);
          break;
        case "thread":
          setThreads((prev) => ({ ...prev, [msg.pid]: msg.messages }));
          break;
        case "event":
          setEvents((prev) =>
            cap(prev, [
              {
                at: msg.at,
                source: msg.source,
                kind: msg.kind,
                target: msg.target,
                consumed: msg.consumed,
              },
            ]),
          );
          break;
        case "log": {
          setLogLines((prev) => cap(prev, [msg.line]));
          const r = ingest(activityState.current, msg.line);
          activityState.current = r.state;
          if (r.entries.length) setActivity((prev) => cap(prev, r.entries));
          break;
        }
        case "provider":
          setProvider(msg.provider);
          break;
      }
    };
    return () => es.close();
  }, [ensureActive]);

  // --- input: message or !shell ---
  const handleSubmit = useCallback(async (text: string) => {
    const pid = activePidRef.current;
    if (pid === null) {
      setStatus("no PAI tab active");
      return;
    }
    if (text.startsWith("!")) {
      const cmd = text.slice(1).trim();
      if (!cmd) {
        setStatus("shell: empty command");
        return;
      }
      appendShell(setShell, pid, [{ kind: "cmd", text: `$ ${cmd}` }]);
      setStatus(`shell: running ${cmd.split(/\s+/)[0]}…`);
      const res = await api.runShell(pid, cmd);
      const entries: ShellEntry[] = res.lines.map((l) => ({
        kind: res.rc === 0 ? "out" : "err",
        text: l,
      }));
      if (res.ctx_applied) entries.push({ kind: "note", text: "context action applied." });
      appendShell(setShell, pid, entries);
      setStatus(`shell: exit ${res.rc}`);
      return;
    }
    await api.sendMessage(pid, text);
    setStatus(`sent → pid ${pid}, waiting for kernel…`);
  }, []);

  // --- keybindings ---
  const selectByIndex = useCallback((i: number) => {
    const f = fleetRef.current;
    if (i >= 0 && i < f.length) setActivePid(f[i].pid);
  }, []);
  const cycle = useCallback((dir: number) => {
    const f = fleetRef.current;
    if (!f.length) return;
    const cur = activePidRef.current;
    const idx = f.findIndex((m) => m.pid === cur);
    const next = f[(idx + dir + f.length) % f.length];
    setActivePid(next.pid);
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((v) => !v);
        return;
      }
      if (e.key === "Escape") {
        if (paletteOpen) {
          setPaletteOpen(false);
          return;
        }
        const pid = activePidRef.current ?? 1;
        api.interrupt(pid);
        setStatus(`interrupt sent → pid ${pid}, cancelled`);
        return;
      }
      if (e.ctrlKey && e.key === "Tab") {
        e.preventDefault();
        cycle(e.shiftKey ? -1 : 1);
        return;
      }
      if (e.ctrlKey && /^[1-9]$/.test(e.key)) {
        e.preventDefault();
        selectByIndex(Number(e.key) - 1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [paletteOpen, cycle, selectByIndex]);

  const onPickProvider = useCallback((key: string) => {
    api.setProvider(key);
    setProvider(key);
    setPaletteOpen(false);
  }, []);

  const messages = activePid !== null ? threads[activePid] ?? [] : [];
  const shellEntries = activePid !== null ? shell[activePid] ?? [] : [];

  return (
    <div className="app">
      <Header connected={connected} />
      <div className="main">
        <div className="chat-col">
          <FleetTabs fleet={fleet} activePid={activePid} onSelect={setActivePid} />
          <ChatPane messages={messages} shell={shellEntries} />
          <StatusBar text={status} />
          <MessageInput disabled={activePid === null} onSubmit={handleSubmit} />
        </div>
        <div className="side-col">
          <section className="panel">
            <div className="panel-label">running procs</div>
            <ProcList rows={procs} />
          </section>
          <section className="panel grow2">
            <div className="panel-label">PAI activity</div>
            <ActivityPane entries={activity} />
          </section>
          <section className="panel">
            <div className="panel-label">events (live)</div>
            <EventStrip events={events} />
          </section>
          <section className="panel grow1">
            <div className="panel-label">kernel.log</div>
            <LogTail lines={logLines} />
          </section>
        </div>
      </div>
      {paletteOpen && (
        <CommandPalette
          provider={provider}
          onPick={onPickProvider}
          onClose={() => setPaletteOpen(false)}
        />
      )}
    </div>
  );
}

function deriveStatus(rows: ProcRow[], pid: number | null): string {
  if (pid === null) return "idle";
  const row = rows.find((r) => r.pid === String(pid));
  if (!row || !row.busy) return "idle";
  const reason = row.busy.reason.trim() || "thinking";
  if (row.busy.started_at > 0) {
    const elapsed = Math.max(0, Math.floor(Date.now() / 1000 - row.busy.started_at));
    return `${row.slug}: ${reason} (${elapsed}s)`;
  }
  return `${row.slug}: ${reason}`;
}

function appendShell(
  setShell: React.Dispatch<React.SetStateAction<Record<number, ShellEntry[]>>>,
  pid: number,
  entries: ShellEntry[],
) {
  setShell((prev) => ({ ...prev, [pid]: (prev[pid] ?? []).concat(entries) }));
}
