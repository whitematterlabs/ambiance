// Browser→kernel writes. These mirror exactly what the TUI writes: a message
// line + an event. Nothing here owns or drives the kernel.

async function post(path: string, body: unknown): Promise<any> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

export const sendMessage = (pid: number, text: string) =>
  post("/api/message", { pid, text });

export const interrupt = (pid: number) => post("/api/interrupt", { pid });

export const runShell = (pid: number, cmd: string) =>
  post("/api/shell", { pid, cmd }) as Promise<{
    ok: boolean;
    lines: string[];
    rc: number;
    ctx_applied: boolean;
  }>;

export const setProvider = (key: string) => post("/api/provider", { key });
