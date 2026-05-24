import type { CSSProperties } from "react";
import { useEffect, useRef } from "react";
import { paiColor } from "../palette";
import type { EventSighting, ProcRow } from "../types";

function eventPai(ev: EventSighting, rows: ProcRow[]): string | null {
  const byPid = new Map(
    rows.filter((r) => r.type === "pai" && r.pid).map((r) => [r.pid, r.slug]),
  );
  const bySlug = new Set(rows.filter((r) => r.type === "pai").map((r) => r.slug));
  const kindMatch = /^pai:([^:]+):/.exec(ev.kind);
  const candidate = ev.pai || kindMatch?.[1] || "";
  if (candidate && byPid.has(candidate)) return byPid.get(candidate) ?? null;
  if (candidate && bySlug.has(candidate)) return candidate;
  if (ev.target && byPid.has(ev.target)) return byPid.get(ev.target) ?? null;
  if (ev.target && bySlug.has(ev.target)) return ev.target;
  return candidate || null;
}

export function EventStrip({
  events,
  procs,
}: {
  events: EventSighting[];
  procs: ProcRow[];
}) {
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
        const pai = eventPai(ev, procs);
        const style = pai ? ({ "--pai-color": paiColor(pai) } as CSSProperties) : undefined;
        return (
          <div key={i} className={`event-line ${pai ? "pai-coded" : ""}`} style={style}>
            <span className="ev-at">{ev.at}</span>{" "}
            <span className={`ev-label ${ev.consumed ? "consumed" : ""}`}>{label}</span>
            {ev.target && <span className="ev-target"> → {ev.target}</span>}
          </div>
        );
      })}
    </div>
  );
}
