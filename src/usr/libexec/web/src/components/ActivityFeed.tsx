import type { CSSProperties } from "react";
import { useEffect, useRef } from "react";
import type { ActivityEntry } from "../activity";
import { paiColor } from "../palette";

// Friendly reskin of the raw activity stream. Each entry's `cls` (produced by
// activity.ts `ingest`) maps to an icon + tone; the text is lightly cleaned for
// display. Pure presentation — `ingest` is untouched.
const META: Record<string, { icon: string; tone: string; mono?: boolean }> = {
  "act-cmd": { icon: "❯", tone: "cmd", mono: true },
  "act-out": { icon: "·", tone: "muted", mono: true },
  "act-ok": { icon: "✓", tone: "ok" },
  "act-pai": { icon: "✦", tone: "pai" },
  "act-nudge": { icon: "→", tone: "nudge" },
  "act-done": { icon: "✓", tone: "ok" },
  "act-dim": { icon: "—", tone: "dim" },
};

// Strip the developer scaffolding the TUI feed carries: a leading [pai:slug] or
// pai:slug: tag, the shell `$ `, nudge `> `/`! ` markers, and indent spaces.
function clean(text: string): string {
  return text
    .replace(/^\[pai(?::[^\]]+)?\]\s*/, "")
    .replace(/^pai(?::[^:]+)?:\s*/, "")
    .replace(/^[$>!]\s+/, "")
    .replace(/^\s+/, "")
    .trim();
}

export function ActivityFeed({ entries }: { entries: ActivityEntry[] }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [entries]);

  if (entries.length === 0) {
    return (
      <div className="activity-feed empty" ref={ref}>
        <div className="feed-empty">Nothing yet — your PAI is quiet.</div>
      </div>
    );
  }

  return (
    <div className="activity-feed" ref={ref}>
      {entries.map((e, i) => {
        const meta = META[e.cls] ?? { icon: "·", tone: "muted" };
        const text = clean(e.text);
        if (!text) return null;
        const style = e.pai
          ? ({ "--pai-color": paiColor(e.pai) } as CSSProperties)
          : undefined;
        return (
          <div
            key={i}
            className={`feed-row tone-${meta.tone} ${e.pai ? "with-pai" : ""}`}
            style={style}
          >
            <span className="feed-icon" aria-hidden="true">
              {meta.icon}
            </span>
            {e.pai && <span className="feed-pai">{e.pai}</span>}
            <span className={`feed-text ${meta.mono ? "mono" : ""}`}>{text}</span>
          </div>
        );
      })}
    </div>
  );
}
