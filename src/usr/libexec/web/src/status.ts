import type { ProcRow } from "./types";

// Wall-clock seconds since a busy span began (clamped at 0). Shared by the
// status line (App) and the friendly StatusCard so both count the same way.
export function elapsedSecs(startedAt: number): number {
  return Math.max(0, Math.floor(Date.now() / 1000 - startedAt));
}

// One-line status string for the active PAI, matching the TUI's status poke.
export function deriveStatus(rows: ProcRow[], pid: number | null): string {
  if (pid === null) return "idle";
  const row = rows.find((r) => r.pid === String(pid));
  if (!row || !row.busy) return "idle";
  const reason = row.busy.reason.trim() || "thinking";
  if (row.busy.started_at > 0) {
    return `${row.slug}: ${reason} (${elapsedSecs(row.busy.started_at)}s)`;
  }
  return `${row.slug}: ${reason}`;
}
