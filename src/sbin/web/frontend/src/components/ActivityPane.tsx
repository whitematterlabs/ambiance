import { useEffect, useRef } from "react";
import type { ActivityEntry } from "../activity";

export function ActivityPane({ entries }: { entries: ActivityEntry[] }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [entries]);

  return (
    <div className="activity-pane scroll" ref={ref}>
      {entries.map((e, i) => (
        <div key={i} className={`act-line ${e.cls}`}>
          {e.text}
        </div>
      ))}
    </div>
  );
}
