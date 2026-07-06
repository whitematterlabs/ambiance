// Browser→backend writes. Chat actions mirror the TUI; kernel lifecycle is
// handled through the web backend's explicit control endpoint.

import { authHeaders, notifyUnauthorized } from "./auth";

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

export const setProvider = (key: string) => post("/api/provider", { key });

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
