// Browser→backend writes. Chat actions mirror the TUI; kernel lifecycle is
// handled through the web backend's explicit control endpoint.

async function post(path: string, body: unknown): Promise<any> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return readJson(res, path);
}

async function get(path: string): Promise<any> {
  const res = await fetch(path);
  return readJson(res, path);
}

async function readJson(res: Response, path: string): Promise<any> {
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

export const sendMessage = (pid: number, text: string) =>
  post("/api/message", { pid, text });

export const interrupt = (pid: number) => post("/api/interrupt", { pid });

export const clonePai = (source: string) =>
  post("/api/clone", { source }) as Promise<{
    ok: boolean;
    source?: string;
    name?: string;
    instance?: string;
    home?: string;
    error?: string;
  }>;

export const runShell = (pid: number, cmd: string) =>
  post("/api/shell", { pid, cmd }) as Promise<{
    ok: boolean;
    lines: string[];
    rc: number;
    ctx_applied: boolean;
  }>;

export const setProvider = (key: string) => post("/api/provider", { key });

export async function transcribeAudio(audio: Blob): Promise<{
  ok: boolean;
  text?: string;
  error?: string;
}> {
  const form = new FormData();
  form.append("audio", audio, `pai-voice.${extensionForAudio(audio.type)}`);
  const res = await fetch("/api/stt", {
    method: "POST",
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
