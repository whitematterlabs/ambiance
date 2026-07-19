import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronRight, Pencil } from "lucide-react";
import type {
  BuildStatus,
  DashboardMeta,
  DriverHealth,
  EventSighting,
  FleetMember,
  KernelStatus,
  ModelsState,
  PendingApproval,
  ProcRow,
  ScheduledTask,
  SendCapability,
  SendMode,
  ServerMessage,
  ShellEntry,
  ThreadMessage,
} from "./types";
import { ActivityEntry, ActivityState, ingest, initialActivity } from "./activity";
import {
  CommandGroup,
  CommandState,
  ingestCommand,
  initialCommands,
  promoteOpenGroup,
} from "./commands";
import { ServerSpeechBackend, SpeechQueue, type VoiceEngine } from "./speech";
import { DEFAULT_WAKE_PHRASE, speechRecognitionSupported, usePhraseActivation } from "./voiceActivation";
import { deriveStatus } from "./status";
import { CAPTURE_FLAGS } from "./capture";
import * as api from "./api";
import { onUnauthorized, setAuthToken, withTokenParam } from "./auth";
import { LoginGate } from "./components/LoginGate";
import { Header } from "./components/Header";
import { MobileMenu } from "./components/MobileMenu";
import { FleetTabs } from "./components/FleetTabs";
import { ChatPane } from "./components/ChatPane";
import { StatusBar } from "./components/StatusBar";
import { MessageInput } from "./components/MessageInput";
import { SidePanel } from "./components/SidePanel";
import { ModelPicker } from "./components/ModelPicker";
import { HeartbeatPicker } from "./components/HeartbeatPicker";
import { MainTabs, dashView, type MainView } from "./components/MainTabs";
import { ScheduledView } from "./components/ScheduledView";
import { DashboardView } from "./components/DashboardView";
import { PlanSidebar, tally } from "./components/PlanSidebar";
import { ScheduleEditor } from "./components/ScheduleEditor";
import { ConfirmDialog } from "./components/ConfirmDialog";
import { BuildBanner } from "./components/BuildBanner";
import { ApprovalModal } from "./components/ApprovalModal";
import { WelcomeDialog } from "./components/WelcomeDialog";

const CAP = 500; // ring-buffer cap for log/activity/events
// How long the browser-fallback phrase listener accepts a wake-free follow-up
// after the PAI finishes talking. Mirrors the host-mic driver's server-armed
// window (actions.open_voice_followup default).
const FOLLOWUP_WINDOW_MS = 12_000;
type MobileView = "chat" | "activity";
type ClearScreen = { label: string; messageIndex: number };

function cap<T>(arr: T[], extra: T[]): T[] {
  const next = arr.concat(extra);
  return next.length > CAP ? next.slice(next.length - CAP) : next;
}

export function App() {
  const [connected, setConnected] = useState(false);
  const [fleet, setFleet] = useState<FleetMember[]>([]);
  const [activePid, setActivePid] = useState<number | null>(null);
  const [procs, setProcs] = useState<ProcRow[]>([]);
  const [threads, setThreads] = useState<Record<number, ThreadMessage[]>>({});
  const [shell, setShell] = useState<Record<number, ShellEntry[]>>({});
  const [events, setEvents] = useState<EventSighting[]>([]);
  const [logLines, setLogLines] = useState<string[]>([]);
  const [activity, setActivity] = useState<ActivityEntry[]>([]);
  // PAI shell commands folded into inline foldable cards (from the log stream).
  const [commands, setCommands] = useState<CommandGroup[]>([]);
  const [status, setStatus] = useState<string>("idle");
  // Inline rename of the active PAI (pencil next to the chat title).
  // null = not editing; a string is the in-progress draft.
  const [renameDraft, setRenameDraft] = useState<string | null>(null);
  // Switching tabs abandons an in-progress rename rather than saving it
  // against the wrong PAI.
  useEffect(() => setRenameDraft(null), [activePid]);
  const [build, setBuild] = useState<BuildStatus | null>(null);
  const [kernel, setKernel] = useState<KernelStatus>({ running: false, pid: null });
  const [kernelBusy, setKernelBusy] = useState(false);
  const [cloningSlugs, setCloningSlugs] = useState<Set<string>>(() => new Set());
  const [deletingSlugs, setDeletingSlugs] = useState<Set<string>>(() => new Set());
  const [killingSlugs, setKillingSlugs] = useState<Set<string>>(() => new Set());
  const [confirmDelete, setConfirmDelete] = useState<FleetMember | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [approvals, setApprovals] = useState<PendingApproval[]>([]);
  const [approvalsOpen, setApprovalsOpen] = useState(false);
  const [sendCaps, setSendCaps] = useState<SendCapability[]>([]);
  const [drivers, setDrivers] = useState<DriverHealth[]>([]);
  const [notetakerRecording, setNotetakerRecording] = useState(false);
  const [models, setModels] = useState<ModelsState | null>(null);
  // Last seen pending count, so the SSE handler can auto-present the modal only
  // when a *new* proposal arrives (count grew), not on every rebroadcast.
  const approvalsCountRef = useRef(0);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [heartbeatOpen, setHeartbeatOpen] = useState(false);
  // Show the welcome/capability tour automatically on the very first boot,
  // then never again unless the owner re-opens it via the header "?" button.
  const [welcomeOpen, setWelcomeOpen] = useState(
    () => localStorage.getItem("welcomeSeen") !== "true",
  );
  const [mobileView, setMobileView] = useState<MobileView>("chat");
  // Top-level center-pane view: Chat or Scheduled Tasks (see MainTabs).
  const [mainView, setMainView] = useState<MainView>("chat");
  // Owner scheduled tasks (paicron jobs). Single source of truth is the hub's
  // `scheduled` SSE broadcast off the /proc watch; edits are optimistic-free —
  // the broadcast reconciles create/edit/delete.
  const [scheduled, setScheduled] = useState<ScheduledTask[]>([]);
  // null = closed; { task: null } = new; { task } = editing.
  const [scheduleEditor, setScheduleEditor] = useState<{ task: ScheduledTask | null } | null>(
    null,
  );
  const [deletingScheduled, setDeletingScheduled] = useState<Set<string>>(() => new Set());
  // PAI-authored dashboards. Single source of truth is the hub's `dashboards`
  // SSE broadcast off the /var/lib/dashboards watch — a file write/delete adds
  // or drops a tab live, no refresh.
  const [dashboards, setDashboards] = useState<DashboardMeta[]>([]);
  // Per-PAI live plan.md (proc/<slug>/plan.md), keyed by pid. Single source of
  // truth is the hub's `plan` SSE broadcast off the /proc watch — a write/tick/
  // rm updates the active PAI's right-rail plan strip live. Absent ⇒ no strip.
  const [plans, setPlans] = useState<Record<number, string>>({});
  // Which right-rail view is active. Lifted out of SidePanel so the sidebar
  // column can widen for the System tab's tables (Activity keeps the slim rail).
  const [panelTab, setPanelTab] = useState<"activity" | "system">("activity");
  // Desktop-only: the left rail (PAI switcher + Activity/System) collapses to
  // hand the chat full width. Persisted so the choice survives reloads.
  const [sidebarOpen, setSidebarOpen] = useState(
    () => localStorage.getItem("sidebarOpen") !== "false",
  );
  // Right plan rail: retracts to a slim pull-tab. Persisted like sidebarOpen.
  const [planOpen, setPlanOpen] = useState(() => localStorage.getItem("planOpen") !== "false");
  const [authNeeded, setAuthNeeded] = useState(false);
  const [clearBusy, setClearBusy] = useState(false);
  const [clearScreens, setClearScreens] = useState<Record<number, ClearScreen>>({});
  const [composerDraft, setComposerDraft] = useState<{ text: string; nonce: number } | null>(
    null,
  );
  const [voiceEnabled, setVoiceEnabled] = useState(
    () => localStorage.getItem("voiceEnabled") === "true",
  );
  const [voiceId, setVoiceId] = useState<string | null>(
    () => localStorage.getItem("voiceId"),
  );
  const [voiceSpeed, setVoiceSpeed] = useState<number>(() => {
    const raw = localStorage.getItem("voiceSpeed");
    const n = raw ? parseFloat(raw) : NaN;
    return Number.isFinite(n) ? n : 1.1;
  });
  // Read-aloud engine: ElevenLabs (premium default) or Siri (macOS `say`). The
  // server falls back to Siri on its own when ElevenLabs has no key.
  const [voiceEngine, setVoiceEngine] = useState<VoiceEngine>(
    () => (localStorage.getItem("voiceEngine") === "siri" ? "siri" : "elevenlabs"),
  );
  // Voice *input* activation modes (independent of the read-aloud toggle above).
  const [pushToTalk, setPushToTalk] = useState(
    () => localStorage.getItem("voicePushToTalk") === "true",
  );
  const [phraseActivation, setPhraseActivation] = useState(
    () => localStorage.getItem("voicePhraseActivation") === "true",
  );
  const [phraseSupported] = useState(speechRecognitionSupported);
  // Whether the host has the local `voice` driver installed (from the hello
  // snapshot). When true, the Phrase-activation switch controls that driver
  // (the host mic) via the kernel — not the browser fallback.
  const [voiceInstalled, setVoiceInstalled] = useState(false);
  // Optimistic override for the host listener while the kernel reconciles the
  // start/stop we just requested — cleared once the proc list catches up, so
  // the switch flips instantly instead of lagging the ~1s reconcile.
  const [hostListenPending, setHostListenPending] = useState<boolean | null>(null);
  // Host-mic voice activity (local `voice` driver), surfaced as a "Speaking: …"
  // composer indicator. Set from the `voice` SSE message; cleared on a timer.
  const [voiceHeard, setVoiceHeard] = useState<{ phase: "listening" | "utterance"; text: string } | null>(
    null,
  );
  const [theme, setTheme] = useState<"light" | "dark">(() => {
    const saved = localStorage.getItem("theme");
    if (saved === "dark" || saved === "light") return saved;
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  });

  const activityState = useRef<ActivityState>(initialActivity());
  const commandState = useRef<CommandState>(initialCommands());
  const activePidRef = useRef<number | null>(null);
  const fleetRef = useRef<FleetMember[]>([]);
  const procsRef = useRef<ProcRow[]>([]);
  const threadsRef = useRef<Record<number, ThreadMessage[]>>({});
  const voiceEnabledRef = useRef(voiceEnabled);
  const lastSpokenLen = useRef<Record<number, number>>({});
  // Was the last owner input spoken (wake phrase / host-mic utterance)? Gates
  // the follow-up window: only voice-initiated exchanges re-open the mic when
  // the PAI finishes talking — a typed chat never hot-mics the room.
  const lastInputVoiceRef = useRef(false);
  // Browser-fallback follow-up window deadline (epoch ms). While in the
  // future, the next final transcript is sent without the wake phrase.
  const followUpUntilRef = useRef(0);
  const pendingCloneSlug = useRef<string | null>(null);
  // After "Set up mobile access", focus root's tab once it appears (root may not
  // be running yet — the nudge wakes it, and applyFleet selects it when it does).
  const pendingFocusPid = useRef<number | null>(null);
  const voiceClearTimer = useRef<number | null>(null);
  const voiceBackend = useRef<ServerSpeechBackend | null>(null);
  if (voiceBackend.current === null) voiceBackend.current = new ServerSpeechBackend();
  const voiceQueue = useRef<SpeechQueue | null>(null);
  if (voiceQueue.current === null) voiceQueue.current = new SpeechQueue(voiceBackend.current);
  // Apply current prefs to the backend on every render — cheap, and keeps the
  // next utterance honest after the user tweaks the dialog mid-session.
  voiceBackend.current.voiceId = voiceId;
  voiceBackend.current.speed = voiceSpeed;
  voiceBackend.current.engine = voiceEngine;
  // Route TTS failures (unavailable backend, upstream 4xx/5xx, playback blocked) to
  // the status bar — otherwise voice mode looks like a no-op when it errors.
  voiceQueue.current.setErrorReporter((msg) => setStatus(msg));
  activePidRef.current = activePid;
  fleetRef.current = fleet;
  procsRef.current = procs;
  threadsRef.current = threads;
  voiceEnabledRef.current = voiceEnabled;

  // Paint the chosen theme onto <html> and remember it for next visit.
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("theme", theme);
  }, [theme]);

  useEffect(() => {
    localStorage.setItem("sidebarOpen", String(sidebarOpen));
  }, [sidebarOpen]);

  useEffect(() => {
    localStorage.setItem("planOpen", String(planOpen));
  }, [planOpen]);

  // Refresh the status line when the active tab changes (TUI pokes /proc).
  useEffect(() => {
    setStatus(deriveStatus(procsRef.current, activePid));
  }, [activePid]);

  const refreshKernel = useCallback(async () => {
    try {
      const next = await api.kernelStatus();
      if (next.ok) {
        setKernel({ running: Boolean(next.running), pid: next.pid ?? null });
        return;
      }
      throw new Error(next.error || "kernel status failed");
    } catch (e) {
      if (e instanceof Error && e.message.includes("/api/kernel returned")) {
        setStatus(`kernel status failed: ${e.message}`);
        return;
      }
      // Fall through to the local inference below. This keeps dev sessions
      // usable when Vite has hot-reloaded but paiweb has not been restarted.
    }
    setKernel({
      running: procsRef.current.length > 0 || fleetRef.current.length > 0,
      pid: null,
    });
  }, []);

  useEffect(() => {
    refreshKernel();
    const id = window.setInterval(refreshKernel, 3000);
    return () => window.clearInterval(id);
  }, [refreshKernel]);

  // Remote tunnel rejected our token (or we have none): pop the login overlay.
  // Any /api/* 401 routes here via api.ts → auth.notifyUnauthorized.
  useEffect(() => {
    onUnauthorized(() => setAuthNeeded(true));
    return () => onUnauthorized(null);
  }, []);

  // Voice mode: persist the toggle, and watermark the active thread so only
  // messages arriving *after* enable (or after a tab switch) are ever spoken —
  // the existing backlog and reconnect snapshots stay silent. Disabling stops
  // playback and drops anything queued.
  useEffect(() => {
    if (voiceId === null) localStorage.removeItem("voiceId");
    else localStorage.setItem("voiceId", voiceId);
  }, [voiceId]);
  useEffect(() => {
    localStorage.setItem("voiceSpeed", String(voiceSpeed));
  }, [voiceSpeed]);
  useEffect(() => {
    localStorage.setItem("voiceEngine", voiceEngine);
  }, [voiceEngine]);
  useEffect(() => {
    localStorage.setItem("voicePushToTalk", String(pushToTalk));
  }, [pushToTalk]);
  useEffect(() => {
    localStorage.setItem("voicePhraseActivation", String(phraseActivation));
  }, [phraseActivation]);

  useEffect(() => {
    localStorage.setItem("voiceEnabled", String(voiceEnabled));
    const queue = voiceQueue.current!;
    if (!voiceEnabled) {
      queue.clear();
      return;
    }
    const pid = activePidRef.current;
    if (pid !== null) {
      lastSpokenLen.current[pid] = (threadsRef.current[pid] ?? []).length;
    }
  }, [voiceEnabled, activePid]);

  // Pick the fallback PAI when no tab is active (matches the TUI default).
  const ensureActive = useCallback((f: FleetMember[]) => {
    setActivePid((cur) => {
      if (cur !== null && f.some((m) => m.pid === cur)) return cur;
      const fb = f.find((m) => m.fallback);
      return fb ? fb.pid : f.length ? f[0].pid : null;
    });
  }, []);

  const applyFleet = useCallback(
    (f: FleetMember[]) => {
      setFleet(f);
      const pending = pendingCloneSlug.current;
      if (pending) {
        const clone = f.find((m) => m.slug === pending);
        if (clone) {
          pendingCloneSlug.current = null;
          setActivePid(clone.pid);
          return;
        }
      }
      const focus = pendingFocusPid.current;
      if (focus !== null && f.some((m) => m.pid === focus)) {
        pendingFocusPid.current = null;
        setActivePid(focus);
        return;
      }
      ensureActive(f);
    },
    [ensureActive],
  );

  // Anchor a PAI command to the tail of its thread the moment it first appears,
  // so its inline card slots into the transcript where it ran (same trick the
  // owner `!cmd` feed uses). Reads live refs — valid for post-hello log lines.
  const anchorFor = useCallback((slug: string): number => {
    const member = slug ? fleetRef.current.find((f) => f.slug === slug) : null;
    const pid = member ? member.pid : activePidRef.current;
    if (pid === null) return 0;
    return (threadsRef.current[pid] ?? []).length;
  }, []);

  // --- SSE stream (kernel → browser) ---
  useEffect(() => {
    // The bundle running in this tab: stamped at release build time, or (dev /
    // unstamped builds) inferred from the first build the server reports —
    // the tab loaded its assets from that same server moments earlier.
    let bundleBuild: string | null = import.meta.env.VITE_PAI_BUILD ?? null;
    // Loaded-bundle staleness: after `pai update` the console *server*
    // re-execs itself into the new release, but this tab keeps running the
    // old JS forever — the skew model (kernel vs console process) never saw
    // the tab at all, so the owner just watched a stale UI with no banner
    // (the "pai update doesn't update the web UI" bug). When the server
    // reports a console build different from the one this bundle came from,
    // reload once per target build (sessionStorage-guarded so a reload that
    // lands back on a stale server can't loop).
    const maybeReloadForBuild = (status: BuildStatus | null | undefined) => {
      const server = status?.console;
      if (!server) return;
      if (bundleBuild === null) {
        bundleBuild = server;
        return;
      }
      if (server === bundleBuild) return;
      const key = "pai-reloaded-for";
      if (sessionStorage.getItem(key) === server) return;
      sessionStorage.setItem(key, server);
      window.location.reload();
    };
    const es = new EventSource(withTokenParam("/api/stream"));
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.onmessage = (e) => {
      const msg: ServerMessage = JSON.parse(e.data);
      switch (msg.type) {
        case "hello": {
          setVoiceInstalled(msg.voice_installed === true);
          setProcs(msg.procs);
          const t: Record<number, ThreadMessage[]> = {};
          for (const [pid, m] of Object.entries(msg.threads)) t[Number(pid)] = m;
          setThreads(t);
          applyFleet(msg.fleet);
          // Seed the approval queue from the snapshot; the badge shows it, but
          // don't auto-pop on connect — only a *new* proposal (a later count
          // increase) presents the modal.
          {
            const pending = msg.pending_approvals ?? [];
            setApprovals(pending);
            approvalsCountRef.current = pending.length;
          }
          setSendCaps(msg.send_capabilities ?? []);
          setScheduled(msg.scheduled ?? []);
          setDrivers(msg.drivers ?? []);
          setDashboards(msg.dashboards ?? []);
          {
            const p: Record<number, string> = {};
            for (const [pid, md] of Object.entries(msg.plans ?? {})) p[Number(pid)] = md;
            setPlans(p);
          }
          setNotetakerRecording(msg.notetaker_recording ?? false);
          setBuild(msg.build ?? null);
          maybeReloadForBuild(msg.build);
          // A hello is a fresh snapshot: drop any command groups from a prior
          // connection before (re)seeding from this backlog.
          commandState.current = initialCommands();
          setCommands([]);
          // Seed the log + activity panes with the kernel.log backlog so a
          // fresh connection isn't a blank "waiting for kernel.log…".
          if (msg.log_backlog?.length) {
            setLogLines(cap([], msg.log_backlog));
            let st = activityState.current;
            const entries: ActivityEntry[] = [];
            for (const line of msg.log_backlog) {
              const r = ingest(st, line);
              st = r.state;
              if (r.entries.length) entries.push(...r.entries);
            }
            activityState.current = st;
            if (entries.length) setActivity((prev) => cap(prev, entries));
            // Seed inline command groups from the same backlog. Anchor to the
            // snapshot thread tails; completed groups stay historical
            // (sidebar-only), and the last still-running one is promoted live so
            // an in-flight command survives the reconnect.
            const slugPid = new Map(msg.fleet.map((f) => [f.slug, f.pid]));
            const seedAnchor = (slug: string): number => {
              const pid = slug ? slugPid.get(slug) : undefined;
              return pid === undefined ? 0 : (t[pid] ?? []).length;
            };
            let cs = initialCommands();
            const seededAt = Date.now();
            for (const line of msg.log_backlog) {
              cs = ingestCommand(cs, line, seededAt, seedAnchor, true);
            }
            cs = promoteOpenGroup(cs);
            commandState.current = cs;
            setCommands(cs.groups);
          }
          break;
        }
        case "procs":
          setProcs(msg.rows);
          setStatus(deriveStatus(msg.rows, activePidRef.current));
          break;
        case "fleet":
          applyFleet(msg.fleet);
          break;
        case "thread":
          setThreads((prev) => ({ ...prev, [msg.pid]: msg.messages }));
          if (voiceEnabledRef.current && msg.pid === activePidRef.current) {
            const queue = voiceQueue.current!;
            const from = lastSpokenLen.current[msg.pid] ?? 0;
            for (const m of msg.messages.slice(from)) {
              if (m.sender !== "pai" || m.raw) continue;
              const text = m.body.replace(/^\s*»\s+/, "").trim();
              if (text) queue.enqueue(text);
            }
            lastSpokenLen.current[msg.pid] = msg.messages.length;
          }
          break;
        case "event":
          setEvents((prev) =>
            cap(prev, [
              {
                at: msg.at,
                source: msg.source,
                kind: msg.kind,
                target: msg.target,
                pai: msg.pai,
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
          const cs = ingestCommand(commandState.current, msg.line, Date.now(), anchorFor);
          if (cs !== commandState.current) {
            commandState.current = cs;
            setCommands(cs.groups);
          }
          break;
        }
        case "build":
          setBuild(msg.status);
          maybeReloadForBuild(msg.status);
          break;
        case "pending_approvals": {
          // The single source of truth for the queue. Auto-present when a new
          // proposal lands (count grew); auto-close when the queue empties. A
          // dismissed-but-still-pending queue stays reachable via the badge.
          const next = msg.approvals;
          setApprovals(next);
          if (next.length > approvalsCountRef.current) setApprovalsOpen(true);
          else if (next.length === 0) setApprovalsOpen(false);
          approvalsCountRef.current = next.length;
          break;
        }
        case "send_capabilities":
          // Full per-channel list, single source of truth — reconciles any
          // optimistic toggle and reflects hand-edits to config.yaml.
          setSendCaps(msg.capabilities);
          break;
        case "drivers":
          // Full per-driver health list — single source of truth, change-gated
          // server-side, so every arrival is a real state change.
          setDrivers(msg.drivers);
          break;
        case "notetaker_recording":
          setNotetakerRecording(msg.recording);
          break;
        case "scheduled":
          // Full owner-task list, change-gated server-side — reconciles any
          // create/edit/delete and reflects a task that just fired or expired.
          setScheduled(msg.tasks);
          break;
        case "dashboards":
          // Full dashboard list, change-gated server-side — a file write/delete
          // adds or drops a tab. If the active dashboard vanished, an effect
          // below rebases the view to Chat.
          setDashboards(msg.dashboards);
          break;
        case "plan": {
          // Full per-PAI plan map, change-gated server-side — reconciles a
          // write/tick, and a `rm`/empty drops that pid so the strip collapses.
          const p: Record<number, string> = {};
          for (const [pid, md] of Object.entries(msg.plans)) p[Number(pid)] = md;
          setPlans(p);
          break;
        }
        case "voice": {
          // Host-mic listener fired. "listening" = wake word landed (no text
          // yet); "utterance" = the phrase was heard (already routed to the PAI
          // by the kernel — the reply lands via the normal thread SSE). We only
          // paint the indicator and surface the heard phrase, then auto-clear.
          if (voiceClearTimer.current !== null) window.clearTimeout(voiceClearTimer.current);
          if (msg.phase === "listening") {
            setVoiceHeard({ phase: "listening", text: "" });
            // Safety net: clear if no utterance follows (silence/false trigger).
            voiceClearTimer.current = window.setTimeout(() => setVoiceHeard(null), 16000);
          } else {
            const heard = (msg.text ?? "").trim();
            setVoiceHeard({ phase: "utterance", text: heard });
            voiceClearTimer.current = window.setTimeout(() => setVoiceHeard(null), 4000);
            // A host-mic utterance means this exchange is voice-driven: when
            // the reply finishes reading aloud, arm the follow-up window.
            lastInputVoiceRef.current = true;
          }
          break;
        }
      }
    };
    return () => es.close();
  }, [applyFleet, anchorFor]);

  // Clear the listening timer if the component unmounts mid-utterance.
  useEffect(
    () => () => {
      if (voiceClearTimer.current !== null) window.clearTimeout(voiceClearTimer.current);
    },
    [],
  );

  // --- input: message or !shell ---
  const handleSubmit = useCallback(async (text: string, options?: { overclock?: boolean; viaVoice?: boolean }) => {
    const pid = activePidRef.current;
    if (pid === null) {
      setStatus("no PAI tab active");
      return;
    }
    // Track how this exchange started: a typed send closes any pending
    // follow-up window; a spoken one keeps the conversation hands-free.
    lastInputVoiceRef.current = options?.viaVoice === true;
    if (options?.viaVoice !== true) followUpUntilRef.current = 0;
    const overclock = options?.overclock === true;
    if (!overclock && text.startsWith("!")) {
      const cmd = text.slice(1).trim();
      if (!cmd) {
        setStatus("shell: empty command");
        return;
      }
      const afterMessageIndex = (threadsRef.current[pid] ?? []).length;
      appendShell(setShell, pid, [{ kind: "cmd", text: `$ ${cmd}` }], afterMessageIndex);
      setStatus(`shell: running ${cmd.split(/\s+/)[0]}…`);
      const res = await api.runShell(pid, cmd);
      const entries: ShellEntry[] = res.lines.map((l) => ({
        kind: res.rc === 0 ? "out" : "err",
        text: l,
      }));
      if (res.ctx_applied) entries.push({ kind: "note", text: "context action applied." });
      appendShell(setShell, pid, entries, afterMessageIndex);
      setStatus(`shell: exit ${res.rc}`);
      return;
    }
    await api.sendMessage(pid, text, overclock);
    setStatus(
      overclock
        ? `overclock sent → pid ${pid}, waiting for kernel…`
        : `sent → pid ${pid}, waiting for kernel…`,
    );
  }, []);

  // Is the local host-mic listener (the `voice` driver's `voice-in` proc) up?
  // When it is, the kernel owns wake detection + STT and routes utterances to
  // the PAI directly — the browser SpeechRecognition fallback below stands down
  // (it would double-fire on the same words) and we ride host-mic SSE instead.
  const localVoiceActive = procs.some(
    (p) => p.slug === "voice-in" && p.status.startsWith("running"),
  );

  // Effective on-state of the host-mic listener: the optimistic pending value
  // while a start/stop is in flight, otherwise the real proc state.
  const hostListening = hostListenPending ?? localVoiceActive;
  // Once the kernel's reconcile lands (proc list matches what we asked for),
  // drop the optimistic override so live proc updates drive the switch again.
  useEffect(() => {
    if (hostListenPending !== null && hostListenPending === localVoiceActive) {
      setHostListenPending(null);
    }
  }, [hostListenPending, localVoiceActive]);

  // The Phrase-activation switch. When the local `voice` driver is installed it
  // is the real off switch for the always-on host mic: toggling start/stops the
  // voice-in driver via the kernel. `phraseActivation` is kept in sync so the
  // browser fallback below can never quietly take over after the host mic is
  // turned off. Without the driver, it's the browser fallback toggle as before.
  const effectivePhraseOn = voiceInstalled ? hostListening : phraseActivation;
  const handleTogglePhrase = useCallback(() => {
    if (voiceInstalled) {
      const next = !hostListening;
      setHostListenPending(next);
      setPhraseActivation(next);
      void api.setVoiceListener(next).catch(() => setHostListenPending(null));
    } else {
      setPhraseActivation((v) => !v);
    }
  }, [voiceInstalled, hostListening]);

  // Follow-up listening: when the PAI finishes talking (read-aloud queue
  // drains) after a voice-initiated exchange, open a short wake-free window so
  // the owner can answer without repeating the wake phrase. Host-mic path arms
  // the driver via the backend; browser fallback arms a local deadline.
  // Reassigned every render (like the backend prefs above) so it closes over
  // fresh phrase/host state.
  voiceQueue.current.onIdle = () => {
    if (!lastInputVoiceRef.current) return;
    if (localVoiceActive) {
      void api.openVoiceFollowup().catch(() => {});
      setStatus("voice: listening for follow-up…");
    } else if (phraseActivation) {
      followUpUntilRef.current = Date.now() + FOLLOWUP_WINDOW_MS;
      setStatus("voice: listening for follow-up…");
    }
  };

  // Hands-free input (cloud/remote fallback): listen for the wake phrase in the
  // browser and send what follows. Stands down when the local host listener is
  // active (it would double-fire on the same words). Muted while PAI is
  // speaking so its own TTS can't trip the wake word.
  usePhraseActivation({
    enabled: phraseActivation && !localVoiceActive,
    phrase: DEFAULT_WAKE_PHRASE,
    onCommand: (text) => {
      followUpUntilRef.current = 0; // one utterance per window; the reply re-arms
      void handleSubmit(text, { viaVoice: true });
    },
    onStatus: setStatus,
    isMuted: () => Boolean(voiceQueue.current?.speaking),
    inFollowUp: () => Date.now() < followUpUntilRef.current,
  });

  const activeMember = activePid !== null ? fleet.find((m) => m.pid === activePid) ?? null : null;
  const activeSlug = activeMember?.slug ?? null;
  const refreshModels = useCallback(() => {
    if (!activeSlug) {
      setModels(null);
      return;
    }
    api.getModels(activeSlug).then(setModels).catch(() => setModels(null));
  }, [activeSlug]);
  useEffect(refreshModels, [refreshModels]);

  const currentModelLabel = useMemo(() => {
    const cur = models?.current;
    if (!cur) return "Model";
    const row = models?.rows.find(
      (r) => r.provider === cur.provider && r.model === cur.model,
    );
    return row?.label ?? cur.model;
  }, [models]);

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
        if (activeSlug) setPickerOpen((v) => !v);
        return;
      }
      if (e.key === "Escape") {
        if (pickerOpen) {
          setPickerOpen(false);
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
  }, [pickerOpen, cycle, selectByIndex, activeSlug]);

  // Optimistic toggle: paint the new mode immediately, then persist. The hub's
  // send_capabilities broadcast is the source of truth and reconciles — if the
  // write fails, the next broadcast (or reconnect) snaps it back.
  const onSetSendMode = useCallback((flag: string, mode: SendMode) => {
    setSendCaps((prev) => prev.map((c) => (c.flag === flag ? { ...c, mode } : c)));
    api.setSendMode(flag, mode);
  }, []);

  // Delete a scheduled task: mark it deleting (spinner), cancel it server-side,
  // and let the `scheduled` broadcast drop it. Idempotent, so no confirm needed.
  const handleDeleteScheduled = useCallback((task: ScheduledTask) => {
    setDeletingScheduled((prev) => new Set(prev).add(task.slug));
    void api
      .deleteScheduled(task.slug)
      .catch(() => undefined)
      .finally(() => {
        setDeletingScheduled((prev) => {
          const next = new Set(prev);
          next.delete(task.slug);
          return next;
        });
      });
  }, []);

  // Capture gates (cowork/notetaker) render as header/mobile-sheet toggles;
  // only the send channels stay in the sidebar permissions rows.
  const captureCaps = sendCaps.filter((c) => CAPTURE_FLAGS.has(c.flag));
  const channelCaps = sendCaps.filter((c) => !CAPTURE_FLAGS.has(c.flag));

  const handleInterrupt = useCallback(() => {
    const pid = activePidRef.current ?? 1;
    api.interrupt(pid);
    setStatus(`interrupt sent → pid ${pid}, cancelled`);
  }, []);

  // Clear is one-shot: queue + apply the history reset for the active PAI. Use
  // bin/clear explicitly because host PATH comes first and `clear` may resolve
  // to the terminal screen-clear command.
  const handleClearContext = useCallback(async () => {
    const pid = activePidRef.current;
    if (pid === null) {
      setStatus("no PAI tab active");
      return;
    }
    setClearBusy(true);
    setStatus("clearing context…");
    try {
      const res = await api.runShell(pid, "bin/clear");
      const last = res.lines[res.lines.length - 1];
      setStatus(
        res.rc === 0
          ? res.ctx_applied
            ? "context cleared"
            : last || "clear queued"
          : `clear: exit ${res.rc}${last ? ` — ${last}` : ""}`,
      );
      if (res.rc === 0 && res.ctx_applied) {
        const ts = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        const messageIndex = (threadsRef.current[pid] ?? []).length;
        setShell((prev) => ({ ...prev, [pid]: [] }));
        setClearScreens((prev) => ({ ...prev, [pid]: { label: ts, messageIndex } }));
      }
    } catch (e) {
      setStatus(`clear failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setClearBusy(false);
    }
  }, []);

  // Compact needs a summary you write, so it can't be one-click: seed the
  // composer with the shell command and let the existing `!` path carry it.
  const handleCompact = useCallback(() => {
    if (activePidRef.current === null) {
      setStatus("no PAI tab active");
      return;
    }
    setComposerDraft({ text: "!compact ", nonce: Date.now() });
    setStatus("compact: add a short summary, then send");
  }, []);

  const handleClone = useCallback(async (member: FleetMember) => {
    const source = member.slug;
    setCloningSlugs((prev) => {
      const next = new Set(prev);
      next.add(source);
      return next;
    });
    setStatus(`cloning ${source}...`);
    try {
      const res = await api.clonePai(source);
      if (!res.ok) throw new Error(res.error || "clone failed");
      if (res.name) pendingCloneSlug.current = res.name;
      setStatus(
        res.name
          ? `cloned ${source} as ${res.name}; waiting for kernel...`
          : `cloned ${source}; waiting for kernel...`,
      );
    } catch (e) {
      setStatus(`clone failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setCloningSlugs((prev) => {
        const next = new Set(prev);
        next.delete(source);
        return next;
      });
    }
  }, []);

  // Delete is a two-step affordance: the "−" opens a confirm dialog; only on
  // confirm do we purge. The fleet SSE drops the tab when its pid disappears,
  // and applyFleet/ensureActive auto-selects another tab if it was active.
  const handleDelete = useCallback((member: FleetMember) => {
    setConfirmDelete(member);
  }, []);

  const runDelete = useCallback(async () => {
    const member = confirmDelete;
    if (!member) return;
    const slug = member.slug;
    setDeleteBusy(true);
    setDeletingSlugs((prev) => {
      const next = new Set(prev);
      next.add(slug);
      return next;
    });
    setStatus(`deleting ${slug}...`);
    try {
      const res = await api.deletePai(slug);
      if (!res.ok) throw new Error(res.error || "delete failed");
      setStatus(`deleted ${slug}`);
    } catch (e) {
      setStatus(`delete failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setConfirmDelete(null);
      setDeleteBusy(false);
      setDeletingSlugs((prev) => {
        const next = new Set(prev);
        next.delete(slug);
        return next;
      });
    }
  }, [confirmDelete]);

  // Kill a subagent: one immediate write (no confirm — it aborts a transient
  // task, nothing is purged). The fleet SSE drops the tab once the kernel reaps
  // the proc; ensureActive then selects another tab if this one was active.
  const handleKill = useCallback(async (member: FleetMember) => {
    const slug = member.slug;
    setKillingSlugs((prev) => {
      const next = new Set(prev);
      next.add(slug);
      return next;
    });
    setStatus(`killing ${slug}...`);
    try {
      const res = await api.killSubagent(slug);
      if (!res.ok) throw new Error(res.error || "kill failed");
      setStatus(`killed ${slug}`);
    } catch (e) {
      setStatus(`kill failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setKillingSlugs((prev) => {
        const next = new Set(prev);
        next.delete(slug);
        return next;
      });
    }
  }, []);

  const handleTranscribeAudio = useCallback(async (audio: Blob) => {
    const res = await api.transcribeAudio(audio);
    if (!res.ok) throw new Error(res.error || "transcription failed");
    return res.text ?? "";
  }, []);

  // Ask root to stand up mobile/remote access (ngrok tunnel). Root may be idle
  // (no tab yet); the nudge wakes it, then we focus its tab so the owner sees
  // root's questions and the QR it generates.
  const handleSetupRemote = useCallback(async () => {
    setStatus("Asking root to set up mobile access…");
    try {
      const res = await api.setupRemote();
      if (!res.ok) throw new Error(res.error || "request failed");
      const pid = res.pid ?? 1;
      if (fleetRef.current.some((m) => m.pid === pid)) {
        pendingFocusPid.current = null;
        setActivePid(pid);
      } else {
        pendingFocusPid.current = pid;
      }
      setStatus("Asked root to set up mobile access — follow along in root's tab.");
    } catch (e) {
      setStatus(`mobile setup failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  }, []);

  const handleToggleKernel = useCallback(async () => {
    setKernelBusy(true);
    setStatus(kernel.running ? "stopping kernel..." : "starting kernel...");
    try {
      const next = kernel.running ? await api.stopKernel() : await api.startKernel();
      if (!next.ok) throw new Error(next.error || "request failed");
      const running = Boolean(next.running);
      const pid = next.pid ?? null;
      setKernel({ running, pid });
      setStatus(running ? `kernel running${pid ? ` (pid ${pid})` : ""}` : "kernel stopped");
    } catch (e) {
      setStatus(`kernel control failed: ${e instanceof Error ? e.message : String(e)}`);
      await refreshKernel();
    } finally {
      setKernelBusy(false);
    }
  }, [kernel.running, refreshKernel]);

  // "+" on the tab bar: new dashboard. Dashboards are root-authored (the
  // make-dashboards skill), so maximize root's chat — Chat view front and
  // center, root's tab focused — and seed the composer with the ask. The normal
  // send path delivers to pid 1 and wakes root even if it has no tab yet (same
  // contract as setup-remote); pendingFocusPid re-focuses it when the tab
  // appears in that case.
  const handleNewDashboard = useCallback(() => {
    setMainView("chat");
    const rootPid = 1;
    pendingFocusPid.current = fleetRef.current.some((m) => m.pid === rootPid) ? null : rootPid;
    setActivePid(rootPid);
    setComposerDraft({ text: "create a dashboard for me: ", nonce: Date.now() });
    setStatus("dashboard: describe what you want, then send");
  }, []);

  // If the active dashboard was deleted (its tab is gone), fall back to Chat so
  // the pane never points at a slug with no tab.
  useEffect(() => {
    if (mainView.startsWith("dash:") && !dashboards.some((d) => dashView(d.slug) === mainView)) {
      setMainView("chat");
    }
  }, [mainView, dashboards]);

  // Live hub state the dashboard bridge pushes into a frame, keyed by channel.
  // v1 exposes the hub snapshot slices the console already holds; PAI-authored
  // data channels extend this later without touching the frame contract.
  const dashboardData = useMemo(
    () => ({ procs, fleet, drivers, scheduled }),
    [procs, fleet, drivers, scheduled],
  );
  const activeDash = mainView.startsWith("dash:")
    ? dashboards.find((d) => dashView(d.slug) === mainView) ?? null
    : null;

  const messages = activePid !== null ? threads[activePid] ?? [] : [];
  // Active PAI's live plan.md. Empty ⇒ the right rail collapses (no strip).
  const activePlan = activePid !== null ? (plans[activePid] ?? "").trim() : "";
  const planTally = useMemo(() => tally(activePlan), [activePlan]);
  // Owner edit of the plan (checkbox toggle, step add/remove, raw edit):
  // optimistic local update, then round-trip through the backend — the hub's
  // /proc watch rebroadcasts the `plan` map and reconciles. An emptied plan
  // deletes the file server-side, which drops the rail on the next broadcast.
  const handlePlanEdit = (md: string) => {
    if (activePid === null) return;
    const pid = activePid;
    setPlans((p) => ({ ...p, [pid]: md }));
    api.writePlan(pid, md).catch((e) => setStatus(`plan save failed: ${e.message}`));
  };
  // "Talk to PAI about it": seed the composer and let the normal send carry it.
  const handlePlanDiscuss = () => {
    setComposerDraft({ text: "About your current plan: ", nonce: Date.now() });
    setStatus("plan: say what you'd like changed, then send");
  };
  const shellEntries = activePid !== null ? shell[activePid] ?? [] : [];
  const clearScreen = activePid !== null ? clearScreens[activePid] ?? null : null;
  const clearOffset = clearScreen ? Math.min(clearScreen.messageIndex, messages.length) : 0;
  const visibleMessages = clearScreen ? messages.slice(clearOffset) : messages;
  const visibleShellEntries = clearScreen
    ? shellEntries
        .filter(
          (entry) =>
            entry.afterMessageIndex !== undefined && entry.afterMessageIndex >= clearOffset,
        )
        .map((entry) => ({
          ...entry,
          afterMessageIndex: Math.max((entry.afterMessageIndex ?? clearOffset) - clearOffset, 0),
        }))
    : shellEntries;
  const activeProc =
    activePid !== null ? procs.find((r) => r.pid === String(activePid)) ?? null : null;
  // Inline command cards for the active PAI only. Completed backlog groups are
  // historical (sidebar owns full history); the clear marker rebases anchors
  // just like the shell feed above.
  const activeCommands =
    activeSlug !== null ? commands.filter((g) => !g.historical && g.slug === activeSlug) : [];
  const visibleCommands = clearScreen
    ? activeCommands
        .filter((g) => g.afterMessageIndex >= clearOffset)
        .map((g) => ({ ...g, afterMessageIndex: Math.max(g.afterMessageIndex - clearOffset, 0) }))
    : activeCommands;
  const activeOverclockRunning = Boolean(
    activeProc?.busy?.reason.trim().startsWith("overclock:"),
  );
  const activeLabel = activeMember?.title || activeMember?.slug || "No active PAI";
  // Rename applies to config-declared fleet members; a subagent's identity is
  // transient (killed, not kept), so it gets no pencil.
  const canRename =
    Boolean(activeMember) && !(activeProc?.type ?? "").startsWith("subagent");
  const commitRename = () => {
    const draft = renameDraft;
    setRenameDraft(null);
    if (draft === null || !activeMember) return;
    const next = draft.trim();
    if (next === activeMember.title) return;
    const slug = activeMember.slug;
    const prevTitle = activeMember.title;
    // Optimistic: paint the new title now; the fleet SSE reconciles once the
    // renamed spec lands (a blank name clears back to the slug).
    setFleet((f) =>
      f.map((m) => (m.slug === slug ? { ...m, title: next || m.slug } : m)),
    );
    api
      .renamePai(slug, next)
      .then((res) => {
        if (!res.ok) throw new Error(res.error || "rename failed");
        setStatus(next ? `renamed ${slug} → ${next}` : `${slug} name reset to slug`);
      })
      .catch((e) => {
        setFleet((f) =>
          f.map((m) => (m.slug === slug ? { ...m, title: prevTitle } : m)),
        );
        setStatus(`rename failed: ${e instanceof Error ? e.message : e}`);
      });
  };
  const activeMeta =
    activeMember && activeProc
      ? `${activeMember.slug} · PID ${activeMember.pid} · ${activeProc.type}`
      : activeMember
        ? `${activeMember.slug} · PID ${activeMember.pid}`
        : "Start the kernel to attach a PAI";

  return (
    <div className="app">
      <BuildBanner build={build} />
      <Header
        connected={connected}
        kernelRunning={kernel.running}
        kernelBusy={kernelBusy}
        onToggleKernel={handleToggleKernel}
        sidebarOpen={sidebarOpen}
        onToggleSidebar={() => setSidebarOpen((v) => !v)}
        theme={theme}
        onToggleTheme={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
        voiceEnabled={voiceEnabled}
        onToggleVoice={() => setVoiceEnabled((v) => !v)}
        voiceId={voiceId}
        voiceSpeed={voiceSpeed}
        onVoiceIdChange={setVoiceId}
        onVoiceSpeedChange={setVoiceSpeed}
        voiceEngine={voiceEngine}
        onVoiceEngineChange={setVoiceEngine}
        onEnableVoice={() => setVoiceEnabled(true)}
        pushToTalk={pushToTalk}
        onTogglePushToTalk={() => setPushToTalk((v) => !v)}
        phraseActivation={effectivePhraseOn}
        onTogglePhraseActivation={handleTogglePhrase}
        phraseSupported={phraseSupported}
        hostManaged={voiceInstalled}
        wakePhrase={DEFAULT_WAKE_PHRASE}
        captureCaps={captureCaps}
        onSetCaptureMode={onSetSendMode}
        onShowWelcome={() => setWelcomeOpen(true)}
        onSetupRemote={handleSetupRemote}
      />
      <MobileMenu
        connected={connected}
        fleet={fleet}
        procs={procs}
        activePid={activePid}
        onSelect={(pid) => {
          setActivePid(pid);
          setMobileView("chat");
        }}
        activeLabel={activeLabel}
        kernelRunning={kernel.running}
        kernelBusy={kernelBusy}
        onToggleKernel={handleToggleKernel}
        theme={theme}
        onToggleTheme={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
        voiceEnabled={voiceEnabled}
        onToggleVoice={() => setVoiceEnabled((v) => !v)}
        voiceId={voiceId}
        voiceSpeed={voiceSpeed}
        onVoiceIdChange={setVoiceId}
        onVoiceSpeedChange={setVoiceSpeed}
        voiceEngine={voiceEngine}
        onVoiceEngineChange={setVoiceEngine}
        onEnableVoice={() => setVoiceEnabled(true)}
        pushToTalk={pushToTalk}
        onTogglePushToTalk={() => setPushToTalk((v) => !v)}
        phraseActivation={effectivePhraseOn}
        onTogglePhraseActivation={handleTogglePhrase}
        phraseSupported={phraseSupported}
        hostManaged={voiceInstalled}
        wakePhrase={DEFAULT_WAKE_PHRASE}
        captureCaps={captureCaps}
        onSetCaptureMode={onSetSendMode}
        onShowWelcome={() => setWelcomeOpen(true)}
        onSetupRemote={handleSetupRemote}
        onClear={handleClearContext}
        clearBusy={clearBusy}
        onCompact={handleCompact}
      />
      <nav className="mobile-view-switch" aria-label="Mobile view">
        <button
          className={`mobile-view-tab ${mobileView === "chat" ? "active" : ""}`}
          type="button"
          aria-pressed={mobileView === "chat"}
          onClick={() => setMobileView("chat")}
        >
          Chat
        </button>
        <button
          className={`mobile-view-tab ${mobileView === "activity" ? "active" : ""}`}
          type="button"
          aria-pressed={mobileView === "activity"}
          onClick={() => setMobileView("activity")}
        >
          Activity
        </button>
      </nav>
      <main
        className="main"
        data-mobile-view={mobileView}
        data-sidebar={sidebarOpen ? "open" : "closed"}
        data-panel={panelTab}
      >
        <aside className="workspace-sidebar" aria-hidden={!sidebarOpen}>
          <div className="sidebar-scroll">
            <section className="sidebar-section">
              <div className="sidebar-heading">PAIs</div>
              <FleetTabs
                variant="rail"
                fleet={fleet}
                activePid={activePid}
                procs={procs}
                onSelect={(pid) => {
                  setActivePid(pid);
                  setMobileView("chat");
                }}
                onClone={handleClone}
                onDelete={handleDelete}
                onKill={handleKill}
                cloningSlugs={cloningSlugs}
                deletingSlugs={deletingSlugs}
                killingSlugs={killingSlugs}
              />
            </section>
            <SidePanel
              tab={panelTab}
              onTabChange={setPanelTab}
              activeProc={activeProc}
              activity={activity}
              procs={procs}
              events={events}
              logLines={logLines}
              sendCaps={channelCaps}
              drivers={drivers}
              onSetSendMode={onSetSendMode}
              onAllowlistChange={(flag, change) =>
                flag === "bash_exec"
                  ? api.updateBashAllowlist(change)
                  : api.updateSendAllowlist(flag.replace(/_send$/, ""), change)
              }
            />
          </div>
        </aside>
        <section className="chat-col">
          <MainTabs
            view={mainView}
            onChange={setMainView}
            dashboards={dashboards}
            onNewDashboard={handleNewDashboard}
          />
          {mainView === "scheduled" ? (
            <ScheduledView
              tasks={scheduled}
              onNew={() => setScheduleEditor({ task: null })}
              onEdit={(t) => setScheduleEditor({ task: t })}
              onDelete={handleDeleteScheduled}
              deletingSlugs={deletingScheduled}
            />
          ) : activeDash ? (
            <DashboardView
              key={activeDash.slug}
              slug={activeDash.slug}
              title={activeDash.title}
              channels={activeDash.channels}
              data={dashboardData}
            />
          ) : (
          <section className="conversation">
            <header className="chat-head">
              <div className="chat-head-copy">
                {renameDraft !== null ? (
                  <input
                    className="chat-title-input"
                    value={renameDraft}
                    autoFocus
                    spellCheck={false}
                    maxLength={60}
                    aria-label="PAI name"
                    onChange={(e) => setRenameDraft(e.target.value)}
                    onBlur={commitRename}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        commitRename();
                      } else if (e.key === "Escape") {
                        // Keep it from the global handler (Escape = interrupt).
                        e.stopPropagation();
                        setRenameDraft(null);
                      }
                    }}
                  />
                ) : (
                  <h1 className="chat-title">
                    <span className="chat-title-text">{activeLabel}</span>
                    {canRename && (
                      <button
                        type="button"
                        className="chat-title-edit"
                        onClick={() => setRenameDraft(activeLabel)}
                        title="Rename this PAI"
                        aria-label={`Rename ${activeLabel}`}
                      >
                        <Pencil size={13} aria-hidden="true" />
                      </button>
                    )}
                  </h1>
                )}
                <p className="chat-meta">{activeMeta}</p>
              </div>
              <div className="chat-head-actions">
                {approvals.length > 0 && (
                  <button
                    className="approval-badge"
                    type="button"
                    onClick={() => setApprovalsOpen(true)}
                    title="Sends awaiting your approval"
                  >
                    {approvals.length} to approve
                  </button>
                )}
                <button
                  className="head-action model-button"
                  type="button"
                  disabled={!activeMember}
                  onClick={() => setPickerOpen(true)}
                  title="Switch this PAI's provider/model (⌘K)"
                >
                  {currentModelLabel}
                </button>
                <button
                  className="head-action"
                  type="button"
                  disabled={!activeMember}
                  onClick={() => setHeartbeatOpen(true)}
                  title="Wake this PAI after it has been idle for an interval"
                >
                  {activeMember?.heartbeat != null
                    ? `Heartbeat · ${activeMember.heartbeat}`
                    : "Heartbeat"}
                </button>
                <button
                  className="head-action"
                  type="button"
                  disabled={activePid === null || clearBusy}
                  onClick={handleClearContext}
                  title="Clear this PAI's conversation buffer (archived, recoverable)"
                >
                  {clearBusy ? "Clearing…" : "Clear"}
                </button>
                <button
                  className="head-action"
                  type="button"
                  disabled={activePid === null}
                  onClick={handleCompact}
                  title="Compact context — distill the conversation into a short summary you write"
                >
                  Compact
                </button>
                <span className={`state-label ${activeProc?.busy ? "busy" : "ready"}`}>
                  {activeProc?.busy ? "Working" : "Ready"}
                </span>
              </div>
            </header>
            <ChatPane
              messages={visibleMessages}
              shell={visibleShellEntries}
              commands={visibleCommands}
              threadKey={activePid}
              busy={activeProc?.busy ?? null}
              clearMarker={clearScreen?.label ?? null}
            />
            <StatusBar text={status} recording={notetakerRecording} />
            <MessageInput
              disabled={activePid === null}
              onSubmit={handleSubmit}
              onInterrupt={handleInterrupt}
              onTranscribeAudio={handleTranscribeAudio}
              onVoiceStatus={setStatus}
              prefill={composerDraft}
              pushToTalk={pushToTalk}
              listening={voiceHeard}
              overclockRunning={activeOverclockRunning}
              ctxTokens={activeProc?.ctx_tokens ?? 0}
              ctxLimit={activeProc?.ctx_limit ?? 0}
            />
            <p className="chat-disclaimer">
              Ambiance is in beta, and the LLM can make mistakes. We do not collect any sort
              of data from you.
            </p>
          </section>
          )}
        </section>
        {activePlan && (
          <aside className={`plan-sidebar${planOpen ? "" : " collapsed"}`} aria-label="Plan">
            <button
              className="plan-pull"
              onClick={() => setPlanOpen((v) => !v)}
              title={planOpen ? "Hide plan" : "Show plan"}
              aria-expanded={planOpen}
            >
              <ChevronRight size={13} className="plan-pull-chevron" />
              <span className="plan-pull-label">
                Plan{planTally.total > 0 ? ` ${planTally.done}/${planTally.total}` : ""}
              </span>
            </button>
            <div className="plan-rail" aria-hidden={!planOpen}>
              <PlanSidebar
                key={activePid}
                plan={activePlan}
                pai={activeSlug}
                onEdit={handlePlanEdit}
                onDiscuss={handlePlanDiscuss}
              />
            </div>
          </aside>
        )}
      </main>
      {pickerOpen && activeSlug && (
        <ModelPicker
          pai={activeSlug}
          onClose={() => setPickerOpen(false)}
          onStatus={setStatus}
          onSwitched={refreshModels}
        />
      )}
      {heartbeatOpen && activeSlug && (
        <HeartbeatPicker
          pai={activeSlug}
          current={activeMember?.heartbeat}
          onClose={() => setHeartbeatOpen(false)}
          onStatus={setStatus}
        />
      )}
      {scheduleEditor && (
        <ScheduleEditor
          task={scheduleEditor.task}
          fleet={fleet}
          defaultPai={activeSlug}
          onClose={() => setScheduleEditor(null)}
        />
      )}
      {confirmDelete && (
        <ConfirmDialog
          title="Delete clone?"
          body={
            <>
              Permanently delete <strong>{confirmDelete.title || confirmDelete.slug}</strong>{" "}
              and all its memory? This can't be undone.
            </>
          }
          busy={deleteBusy}
          onConfirm={runDelete}
          onCancel={() => setConfirmDelete(null)}
        />
      )}
      {approvalsOpen && approvals.length > 0 && (
        <ApprovalModal
          approvals={approvals}
          onApprove={(id, body) => api.approve(id, body)}
          onReject={(id, r) => api.reject(id, r)}
          onAlwaysAllow={(id, body) =>
            // The server appends the derived rule(s) before flipping the
            // record, so if the approve races a timeout the rule still lands
            // and the PAI's retry sails through.
            api.approve(id, body, true)
          }
          onClose={() => setApprovalsOpen(false)}
        />
      )}
      {welcomeOpen && (
        <WelcomeDialog
          onClose={() => {
            localStorage.setItem("welcomeSeen", "true");
            setWelcomeOpen(false);
          }}
        />
      )}
      {authNeeded && (
        <LoginGate
          onSubmit={(code) => {
            setAuthToken(code);
            // Reload so the SSE stream + every poll re-issue with the new token.
            window.location.reload();
          }}
        />
      )}
    </div>
  );
}

function appendShell(
  setShell: React.Dispatch<React.SetStateAction<Record<number, ShellEntry[]>>>,
  pid: number,
  entries: ShellEntry[],
  afterMessageIndex: number,
) {
  setShell((prev) => ({
    ...prev,
    [pid]: (prev[pid] ?? []).concat(entries.map((e) => ({ ...e, afterMessageIndex }))),
  }));
}
