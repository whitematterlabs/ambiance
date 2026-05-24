import type { CSSProperties } from "react";
import type { FleetMember, ProcRow } from "../types";
import { paiColor } from "../palette";

export function FleetTabs({
  fleet,
  activePid,
  procs,
  onSelect,
  onClone,
  cloningSlugs,
}: {
  fleet: FleetMember[];
  activePid: number | null;
  procs: ProcRow[];
  onSelect: (pid: number) => void;
  onClone: (member: FleetMember) => void;
  cloningSlugs: Set<string>;
}) {
  if (!fleet.length) {
    return <div className="fleet-strip empty">No running PAIs</div>;
  }
  const busyPids = new Set(procs.filter((r) => r.busy).map((r) => r.pid));
  return (
    <div className="fleet-strip" role="tablist">
      {fleet.map((m) => {
        const busy = busyPids.has(String(m.pid));
        const cloning = cloningSlugs.has(m.slug);
        const label = m.title || m.slug;
        const style = { "--pai-color": paiColor(m.slug || m.pid) } as CSSProperties;
        return (
          <div key={m.pid} className="fleet-tab-shell" style={style}>
            <button
              type="button"
              role="tab"
              aria-selected={m.pid === activePid}
              className={`fleet-tab ${m.pid === activePid ? "active" : ""} ${busy ? "busy" : ""}`}
              onClick={() => onSelect(m.pid)}
              title={m.fallback ? "Default (owner-facing) PAI" : ""}
            >
              <span className="fleet-tab-name">
                <span className="fleet-tab-dot" aria-hidden="true" />
                {label}
              </span>
              <span className="fleet-tab-meta">
                {busy ? "Working" : "Ready"} / PID {m.pid}
                {m.fallback ? " / Default" : ""}
              </span>
            </button>
            <button
              type="button"
              className="fleet-clone-button"
              onClick={() => onClone(m)}
              disabled={cloning}
              aria-label={`Clone ${label}`}
              title="Clone this PAI"
            >
              {cloning ? (
                <span className="fleet-clone-spinner" aria-hidden="true" />
              ) : (
                <span aria-hidden="true">+</span>
              )}
            </button>
          </div>
        );
      })}
    </div>
  );
}
