// Browser→backend writes. Chat actions mirror the TUI; kernel lifecycle is
// handled through the web backend's explicit control endpoint.

import { authHeaders, notifyUnauthorized } from "./auth";
import type { DashboardMeta, ModelsState, ScheduledTask } from "./types";

async function post(path: string, body: unknown): Promise<any> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  return readJson(res, path);
}

async function get(path: string): Promise<any> {
  const res = await fetch(path, { headers: authHeaders() });
  return readJson(res, path);
}

async function readJson(res: Response, path: string): Promise<any> {
  if (res.status === 401) {
    // Remote tunnel rejected us — surface the login overlay and let callers
    // treat it as a soft failure (they fall back to local inference).
    notifyUnauthorized();
    throw new Error(`${path} returned 401 (unauthorized)`);
  }
  const text = await res.text();
  if (!text.trim()) return {};
  try {
    return JSON.parse(text);
  } catch {
    const contentType = res.headers.get("content-type") || "unknown content";
    const kind = contentType.includes("text/html") ? "HTML" : contentType;
    throw new Error(
      `${path} returned ${kind}, not JSON. Restart pai-web so the API routes are active.`,
    );
  }
}

export const sendMessage = (pid: number, text: string, overclock = false) =>
  post("/api/message", { pid, text, ...(overclock ? { overclock: true } : {}) });

export const interrupt = (pid: number) => post("/api/interrupt", { pid });

// Start/stop the local host-mic wake-word listener (the `voice-in` driver).
// `present: false` means the voice driver isn't installed on the host.
export const setVoiceListener = (active: boolean) =>
  post("/api/voice-listener", { active }) as Promise<{
    ok: boolean;
    present: boolean;
    active: boolean;
  }>;

// Arm a wake-free follow-up window on the host-mic listener — called when the
// PAI's read-aloud reply finishes playing, so the owner can answer without
// repeating the wake word. `present: false` means the voice driver isn't installed.
export const openVoiceFollowup = () =>
  post("/api/voice-followup", {}) as Promise<{
    ok: boolean;
    present: boolean;
    armed: boolean;
  }>;

// Ask root (the privileged system PAI) to stand up mobile/remote access via an
// ngrok tunnel. Returns root's pid so the UI can focus its tab.
export const setupRemote = () =>
  post("/api/setup-remote", {}) as Promise<{ ok: boolean; pid?: number; error?: string }>;

export const clonePai = (source: string) =>
  post("/api/clone", { source }) as Promise<{
    ok: boolean;
    source?: string;
    name?: string;
    instance?: string;
    home?: string;
    error?: string;
  }>;

export const deletePai = (name: string) =>
  post("/api/delete", { name }) as Promise<{
    ok: boolean;
    name?: string;
    home?: string;
    instance?: string;
    purged?: boolean;
    error?: string;
  }>;

// Abort a running subagent (owner-initiated). The fleet SSE drops its tab once
// the kernel reaps the proc.
export const killSubagent = (name: string) =>
  post("/api/kill", { name }) as Promise<{
    ok: boolean;
    name?: string;
    error?: string;
  }>;

// Owner edit of a PAI's live plan.md (checkbox toggle, step add/remove, raw
// edit). Empty content deletes the file. The hub's /proc watch rebroadcasts the
// `plan` map, so callers update optimistically and let the SSE reconcile.
export const writePlan = (pid: number, content: string) =>
  post("/api/plan", { pid, content }) as Promise<{ ok: boolean; error?: string }>;

export const runShell = (pid: number, cmd: string) =>
  post("/api/shell", { pid, cmd }) as Promise<{
    ok: boolean;
    lines: string[];
    rc: number;
    ctx_applied: boolean;
  }>;

// Draft & approve: the owner decides a queued send. The hub's file watcher
// rebroadcasts the shrunken pending list — these don't mutate local state.
export const approve = (id: string, body?: string) =>
  post("/api/approve", { id, ...(body !== undefined ? { body } : {}) }) as Promise<{
    ok: boolean;
    id?: string;
    status?: string;
    error?: string;
  }>;

export const reject = (id: string, reason: string) =>
  post("/api/reject", { id, reason }) as Promise<{ ok: boolean; id?: string; status?: string; error?: string }>;

// Model picker: catalog + key status (GET), per-PAI switch (POST), key entry.
export const getModels = (pai: string | null) =>
  get(`/api/models${pai ? `?pai=${encodeURIComponent(pai)}` : ""}`) as Promise<
    ModelsState & { ok: boolean }
  >;
export const setModel = (pai: string, provider: string, model: string) =>
  post("/api/models", { pai, provider, model });

// Rename a fleet PAI (owner-facing display name; the slug is untouched). The
// backend rewrites config.yaml and reloads the kernel; the fleet SSE reconciles
// the optimistic local update once the new spec lands.
export const renamePai = (pai: string, displayName: string) =>
  post("/api/rename", { pai, display_name: displayName }) as Promise<{
    ok: boolean;
    name?: string;
    display_name?: string;
    error?: string;
  }>;
export const setApiKey = (provider: string, key: string) =>
  post("/api/apikey", { provider, key });

// Set a PAI's idle heartbeat interval ("30m"/"2h"); null turns it off. The
// backend rewrites config.yaml and reloads the kernel; the fleet SSE
// reconciles the optimistic local update once the new spec lands.
export const setHeartbeat = (pai: string, heartbeat: string | null) =>
  post("/api/heartbeat", { pai, heartbeat }) as Promise<{
    ok: boolean;
    name?: string;
    heartbeat?: string | null;
    error?: string;
  }>;

// Set a send channel's tri-state mode (no/ask/yes). The backend rewrites
// capabilities in config.yaml and reloads the kernel; the hub then rebroadcasts
// send_capabilities, so callers update optimistically and let it reconcile.
export const setSendMode = (flag: string, mode: string) =>
  post("/api/send-mode", { flag, mode }) as Promise<{
    ok: boolean;
    flag?: string;
    mode?: string;
    error?: string;
  }>;

// ElevenLabs key management for the voice dropdown. The backend persists the
// key into $PAI_ROOT/.env and only ever returns a masked hint (last 4 chars).
export const elevenLabsKeyStatus = () =>
  get("/api/elevenlabs-key") as Promise<{
    ok: boolean;
    set?: boolean;
    hint?: string | null;
    error?: string;
  }>;

export const setElevenLabsKey = (key: string) =>
  post("/api/elevenlabs-key", { key }) as Promise<{
    ok: boolean;
    set?: boolean;
    hint?: string | null;
    error?: string;
  }>;

export async function transcribeAudio(audio: Blob): Promise<{
  ok: boolean;
  text?: string;
  error?: string;
}> {
  const form = new FormData();
  form.append("audio", audio, `pai-voice.${extensionForAudio(audio.type)}`);
  const res = await fetch("/api/stt", {
    method: "POST",
    headers: authHeaders(),
    body: form,
  });
  return readJson(res, "/api/stt");
}

// Scheduled tasks: owner-editable paicron jobs. The body carries structured
// fields (repeat/time/dow/date/instruction) — the server owns cron strings. The
// hub's /proc watch rebroadcasts the full `scheduled` list, so create/edit/
// delete are optimistic and reconciled by that broadcast.
export interface ScheduleBody {
  pai: string;
  repeat: string;
  time: string;
  dow?: number | null;
  date?: string | null;
  instruction: string;
}

export const listScheduled = () =>
  get("/api/scheduled") as Promise<{ ok: boolean; tasks?: ScheduledTask[]; error?: string }>;

export const addScheduled = (body: ScheduleBody) =>
  post("/api/scheduled", body) as Promise<{ ok: boolean; task?: ScheduledTask; error?: string }>;

export const updateScheduled = (slug: string, body: ScheduleBody) =>
  post("/api/scheduled/update", { slug, ...body }) as Promise<{
    ok: boolean;
    task?: ScheduledTask;
    error?: string;
  }>;

export const deleteScheduled = (slug: string) =>
  post("/api/scheduled/delete", { slug }) as Promise<{
    ok: boolean;
    slug?: string;
    status?: string;
    error?: string;
  }>;

// PAI-authored dashboards: the tab list (slug/title/order/channels). The hub's
// /var/lib/dashboards watch rebroadcasts the full `dashboards` list live, so
// this GET is only the poke-it-with-curl mirror; the SSE stream is the live path.
export const listDashboards = () =>
  get("/api/dashboards") as Promise<{ ok: boolean; dashboards?: DashboardMeta[]; error?: string }>;

export const kernelStatus = () =>
  get("/api/kernel") as Promise<{
    ok: boolean;
    running?: boolean;
    pid?: string | null;
    error?: string;
  }>;

export const startKernel = () =>
  post("/api/kernel", { action: "start" }) as Promise<{
    ok: boolean;
    running?: boolean;
    pid?: string | null;
    error?: string;
  }>;

export const stopKernel = () =>
  post("/api/kernel", { action: "stop" }) as Promise<{
    ok: boolean;
    running?: boolean;
    pid?: string | null;
    error?: string;
  }>;

function extensionForAudio(type: string): string {
  if (type.includes("mp4")) return "mp4";
  if (type.includes("mpeg")) return "mp3";
  if (type.includes("ogg")) return "ogg";
  if (type.includes("wav")) return "wav";
  return "webm";
}
