export interface FleetMember {
  pid: number;
  slug: string;
  fallback: boolean;
  title: string;
  // Source PAI this was cloned from, or null/undefined for originals. Consumed
  // straight off the wire (snake_case, like the other server-sourced fields);
  // gates the "−" delete button — only clones are deletable.
  clone_of?: string | null;
}

export interface ProcRow {
  slug: string;
  pid: string;
  type: string;
  parent: string;
  when: string;
  when_short: string;
  description: string;
  status: string;
  tree_prefix: string;
  busy: { reason: string; started_at: number } | null;
  ctx_tokens: number;
  // Token count at which the kernel forces a compaction — the "full" mark for
  // the composer's context ring. Sourced from per-PAI `compact_threshold` or
  // the kernel default.
  ctx_limit: number;
}

export interface ThreadMessage {
  ts: string;
  sender: string;
  body: string;
  raw: boolean;
}

export interface EventSighting {
  at: string;
  source: string;
  kind: string;
  target: string;
  pai?: string;
  consumed: boolean;
}

export interface KernelStatus {
  running: boolean;
  pid: string | null;
}

// A transient shell entry shown in the chat pane (never persisted to a thread).
export interface ShellEntry {
  kind: "cmd" | "out" | "err" | "exit" | "note";
  text: string;
  afterMessageIndex?: number;
}

// One queued outbound send awaiting the owner's decision (draft & approve).
// The review projection the server ships — never the raw record, never a token.
export interface PendingApproval {
  id: string;
  channel: string;
  created_by: string;
  created_at: string;
  recipient?: string;
  subject?: string;
  body?: string;
}

// One mounted send channel and its current tri-state permission. Drives the
// sidebar's Send permissions control; only channels a PAI can actually use ship.
export type SendMode = "no" | "ask" | "yes";
export interface SendCapability {
  flag: string;
  channel: string;
  mode: SendMode;
}

export type ServerMessage =
  | { type: "hello"; provider: string; fleet: FleetMember[]; procs: ProcRow[]; pending_approvals?: PendingApproval[]; send_capabilities?: SendCapability[]; threads: Record<string, ThreadMessage[]>; log_backlog?: string[] }
  | { type: "procs"; rows: ProcRow[] }
  | { type: "fleet"; fleet: FleetMember[] }
  | { type: "thread"; pid: number; messages: ThreadMessage[] }
  | { type: "event"; at: string; source: string; kind: string; target: string; pai?: string; consumed: boolean }
  | { type: "log"; line: string }
  | { type: "provider"; provider: string }
  // Host-mic voice activity forwarded from the kernel: "listening" the instant
  // the wake word fires (no text yet), "utterance" once the phrase is
  // transcribed (the kernel already routed it to the PAI — this is display-only).
  | { type: "voice"; phase: "listening" | "utterance"; text?: string }
  // The owner approval queue changed — full pending list, single source of truth.
  | { type: "pending_approvals"; approvals: PendingApproval[] }
  // Send permissions changed (toggle or hand-edit) — full list per channel.
  | { type: "send_capabilities"; capabilities: SendCapability[] };
