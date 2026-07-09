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

// One mounted capability and its current permission. Drives the sidebar's
// Permissions control; only channels a PAI can actually use ship. `modes` is
// the flag's allowed set — send channels are no/ask/yes, capture gates
// (Cowork Mode, Notetaker) are two-state no/yes.
export type SendMode = "no" | "ask" | "yes";
export interface SendCapability {
  flag: string;
  channel: string;
  mode: SendMode;
  modes?: SendMode[];
}

// One kernel-supervised driver process and its health classification, as
// aggregated by the backend (proc status + supervision breadcrumbs + /sys
// state mtimes). `last_activity` is epoch seconds — the client derives the
// live "3h ago" at render time; `state` is the backend's classification and
// flips via rebroadcast when the disk facts (or a staleness window) change.
export type DriverState = "ok" | "stale" | "down" | "looping" | "off";
export interface DriverHealth {
  slug: string;
  driver: string;
  active: boolean;
  status: string;
  starts: number;
  last_start: string | null;
  last_exit: string | null;
  last_exit_outcome: string | null;
  last_exit_reason: string;
  last_activity: number | null;
  stale_after_s: number | null;
  state: DriverState;
  state_reason: string;
}

// One owner-created scheduled task = a paicron proc (schedule + description +
// parent pid, no run). The server owns all cron-string logic and ships the
// structured fields the editor round-trips plus a human `label` and `next_fire`
// (local ISO, or null for a one-shot already past). `repeat: "custom"` marks a
// hand-written cron the presets can't represent — shown read-only.
export type ScheduleRepeat = "once" | "daily" | "weekdays" | "weekly" | "custom";
export interface ScheduledTask {
  slug: string;
  pai: string;
  parent: number | null;
  instruction: string;
  schedule: string;
  repeat: ScheduleRepeat;
  time: string | null;
  dow: number | null;
  date: string | null;
  label: string;
  next_fire: string | null;
}

// Build-skew status: which build the kernel vs this console is running, and
// whether the kernel is old enough that the console is auto-rebooting it.
export type BuildSkew = "unknown" | "in_sync" | "kernel_stale" | "console_stale" | "both_stale";
export interface BuildStatus {
  state: BuildSkew;
  kernel: string | null;
  console: string;
  current: string;
  escalated: boolean;
}

export type ServerMessage =
  | { type: "hello"; voice_installed?: boolean; fleet: FleetMember[]; procs: ProcRow[]; pending_approvals?: PendingApproval[]; scheduled?: ScheduledTask[]; send_capabilities?: SendCapability[]; drivers?: DriverHealth[]; notetaker_recording?: boolean; threads: Record<string, ThreadMessage[]>; log_backlog?: string[]; build?: BuildStatus }
  | { type: "build"; status: BuildStatus }
  | { type: "procs"; rows: ProcRow[] }
  | { type: "fleet"; fleet: FleetMember[] }
  | { type: "thread"; pid: number; messages: ThreadMessage[] }
  | { type: "event"; at: string; source: string; kind: string; target: string; pai?: string; consumed: boolean }
  | { type: "log"; line: string }
  // Host-mic voice activity forwarded from the kernel: "listening" the instant
  // the wake word fires (no text yet), "utterance" once the phrase is
  // transcribed (the kernel already routed it to the PAI — this is display-only).
  | { type: "voice"; phase: "listening" | "utterance"; text?: string }
  // The owner approval queue changed — full pending list, single source of truth.
  | { type: "pending_approvals"; approvals: PendingApproval[] }
  // Send permissions changed (toggle or hand-edit) — full list per channel.
  | { type: "send_capabilities"; capabilities: SendCapability[] }
  // Driver health changed (state flip, restart, exit) — full list, single
  // source of truth, change-gated by the hub.
  | { type: "drivers"; drivers: DriverHealth[] }
  | { type: "notetaker_recording"; recording: boolean }
  // Owner scheduled tasks changed (create/edit/delete) — full list, single
  // source of truth, change-gated by the hub off the /proc watch.
  | { type: "scheduled"; tasks: ScheduledTask[] };

export interface ModelRow {
  provider: string;
  model: string;
  label: string;
  tag: string | null;
  key_status: "found" | "missing";
}

export interface ModelsState {
  rows: ModelRow[];
  providers: Record<
    string,
    { key_status: "found" | "missing"; api_key_env: string; default_model: string }
  >;
  current: { pai: string; provider: string; model: string } | null;
}
