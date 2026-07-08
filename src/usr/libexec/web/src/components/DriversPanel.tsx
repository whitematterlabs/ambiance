import { useEffect, useState } from "react";
import type { DriverHealth } from "../types";

// Compact age: 42s / 12m / 3h / 5d. Timestamps come over the wire; the age is
// derived here at render time (the panel ticks, the payload doesn't).
export function fmtAge(epochSecs: number, nowMs: number): string {
  const s = Math.max(0, Math.floor(nowMs / 1000 - epochSecs));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

const STATE_LABEL: Record<DriverHealth["state"], string> = {
  ok: "ok",
  stale: "stale",
  down: "down",
  looping: "looping",
  off: "off",
};

// Hover detail: the classification reason plus the last exit breadcrumb, so a
// red row explains itself without leaving the panel.
function rowTitle(d: DriverHealth): string {
  const parts: string[] = [];
  if (d.state_reason) parts.push(d.state_reason);
  if (d.last_exit && d.last_exit_outcome) {
    parts.push(
      `last exit ${d.last_exit} (${d.last_exit_outcome}${d.last_exit_reason ? `: ${d.last_exit_reason}` : ""})`,
    );
  }
  if (d.last_start) parts.push(`last start ${d.last_start}`);
  return parts.join("\n");
}

// One row per kernel-supervised driver process. Quiet ink dot when healthy;
// the console's restrained warn/fail tones when a driver is stale or dead —
// the whole point is that "silently broken for weeks" becomes a red dot today.
export function DriversPanel({ drivers }: { drivers: DriverHealth[] }) {
  // Tick twice a minute so the activity ages stay honest while the row data
  // itself only changes on a real broadcast.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(id);
  }, []);

  return (
    <div className="driver-list">
      <table>
        <thead>
          <tr>
            <th></th>
            <th>driver</th>
            <th>state</th>
            <th>restarts</th>
            <th>activity</th>
          </tr>
        </thead>
        <tbody>
          {drivers.length === 0 && (
            <tr className="empty-row">
              <td colSpan={5}>no drivers installed</td>
            </tr>
          )}
          {drivers.map((d) => (
            <tr key={d.slug} className={`driver-row driver-${d.state}`} title={rowTitle(d)}>
              <td className="driver-dot-cell">
                <span className={`driver-dot driver-dot-${d.state}`} aria-hidden="true" />
              </td>
              <td className="slug">{d.slug}</td>
              <td className="driver-state">
                {STATE_LABEL[d.state] ?? d.state}
                {d.state_reason && (
                  <span className="driver-reason"> · {d.state_reason}</span>
                )}
              </td>
              <td className="driver-restarts">
                {d.starts > 1 ? `↻ ${d.starts - 1}` : "-"}
              </td>
              <td className="driver-activity">
                {d.last_activity !== null ? `${fmtAge(d.last_activity, now)} ago` : "-"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
