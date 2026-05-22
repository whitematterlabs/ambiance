import { useEffect, useRef } from "react";
import type { EventSighting } from "../types";

export function EventStrip({ events }: { events: EventSighting[] }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [events]);

  return (
    <div className="event-strip scroll" ref={ref}>
      {events.length === 0 && <div className="feed-empty">no events yet</div>}
      {events.map((ev, i) => {
        // label = kind if already prefixed with "source:", else "source:kind".
        const label = ev.kind.startsWith(`${ev.source}:`)
          ? ev.kind
          : `${ev.source}:${ev.kind}`;
        return (
          <div key={i} className="event-line">
            <span className="ev-at">{ev.at}</span>{" "}
            <span className={`ev-label ${ev.consumed ? "consumed" : ""}`}>{label}</span>
            {ev.target && <span className="ev-target"> → {ev.target}</span>}
          </div>
        );
      })}
    </div>
  );
}
