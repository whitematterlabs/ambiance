import { useEffect, useState } from "react";
import { elapsedSecs } from "../status";
import { humanizeStep } from "../working";

export function WorkingIndicator({
  busy,
}: {
  busy: { reason: string; started_at: number };
}) {
  const [, setTick] = useState(0);

  useEffect(() => {
    const id = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, []);

  const step = humanizeStep(busy.reason);
  const secs = busy.started_at > 0 ? elapsedSecs(busy.started_at) : 0;

  return (
    <div className="working-indicator" aria-live="polite">
      <span className="working-dot" />
      <span className="working-verb">{step.verb}</span>
      {step.detail && <span className="working-detail">{step.detail}</span>}
      {busy.started_at > 0 && <span className="working-elapsed">({secs}s)</span>}
    </div>
  );
}
