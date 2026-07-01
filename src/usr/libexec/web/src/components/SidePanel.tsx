import { useState } from "react";
import type { EventSighting, ProcRow, SendCapability, SendMode } from "../types";
import type { ActivityEntry } from "../activity";
import { StatusCard } from "./StatusCard";
import { SendPermissions } from "./SendPermissions";
import { ActivityFeed } from "./ActivityFeed";
import { ProcList } from "./ProcList";
import { EventStrip } from "./EventStrip";
import { LogTail } from "./LogTail";

type Tab = "activity" | "system";

// The right rail: a friendly Activity view (default) and a System view that
// preserves the raw process / event / log tables for power users. Both read
// data already in App state — no kernel behavior here.
export function SidePanel({
  activeProc,
  activity,
  procs,
  events,
  logLines,
  sendCaps,
  onSetSendMode,
}: {
  activeProc: ProcRow | null;
  activity: ActivityEntry[];
  procs: ProcRow[];
  events: EventSighting[];
  logLines: string[];
  sendCaps: SendCapability[];
  onSetSendMode: (flag: string, mode: SendMode) => void;
}) {
  const [tab, setTab] = useState<Tab>("activity");

  return (
    <aside className={`side-panel ${tab === "system" ? "system-open" : "activity-open"}`}>
      <div className="segmented" role="tablist" aria-label="Side panel view">
        <button
          role="tab"
          aria-selected={tab === "activity"}
          className={`segment ${tab === "activity" ? "active" : ""}`}
          onClick={() => setTab("activity")}
        >
          Activity
        </button>
        <button
          role="tab"
          aria-selected={tab === "system"}
          className={`segment ${tab === "system" ? "active" : ""}`}
          onClick={() => setTab("system")}
        >
          System
        </button>
      </div>

      {tab === "activity" ? (
        <div className="side-body activity-body">
          <StatusCard proc={activeProc} />
          <SendPermissions capabilities={sendCaps} onSetMode={onSetSendMode} />
          <div className="sys-block grow">
            <div className="sys-head">Recent activity</div>
            <ActivityFeed entries={activity} />
          </div>
        </div>
      ) : (
        <div className="side-body system-body">
          <div className="sys-block">
            <div className="sys-head">
              <span>Processes</span>
              <span className="sys-count">{procs.length}</span>
            </div>
            <ProcList rows={procs} />
          </div>
          <div className="sys-block">
            <div className="sys-head">
              <span>Events</span>
              <span className="sys-count">{events.length}</span>
            </div>
            <EventStrip events={events} procs={procs} />
          </div>
          <div className="sys-block grow">
            <div className="sys-head">
              <span>Kernel log</span>
              <span className="sys-count">{logLines.length}</span>
            </div>
            <LogTail lines={logLines} />
          </div>
        </div>
      )}
    </aside>
  );
}
