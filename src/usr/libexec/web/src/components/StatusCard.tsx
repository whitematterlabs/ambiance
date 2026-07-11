import { useEffect, useState } from "react";
import type { ProcRow } from "../types";
import { elapsedSecs } from "../status";

// "What your PAI is doing right now." Renders nothing when idle; shows a
// live elapsed counter while busy. Pure read of activeProc.busy — no kernel writes.
export function StatusCard({ proc }: { proc: ProcRow | null }) {
  const busy = proc?.busy ?? null;

  // Tick once a second while busy so the elapsed counter stays live even
  // between kernel `procs` pushes. Idle => no timer.
  const [, force] = useState(0);
  useEffect(() => {
    if (!busy) return;
    const id = setInterval(() => force((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [busy]);

  if (!busy) {
    return null;
  }

  const reason = busy.reason.trim() || "thinking";
  const elapsed = busy.started_at > 0 ? `${elapsedSecs(busy.started_at)}s` : null;
  return (
    <div className="status-card working">
      <div className="status-card-copy">
        <span className="status-card-title">Working</span>
        <span className="status-card-sub">
          {reason}
          {elapsed && <span className="status-card-elapsed"> · {elapsed}</span>}
        </span>
      </div>
    </div>
  );
}
