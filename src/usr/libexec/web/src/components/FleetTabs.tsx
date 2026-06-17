import type { CSSProperties } from "react";
import type { FleetMember, ProcRow } from "../types";
import { paiColor } from "../palette";

export function FleetTabs({
  fleet,
  activePid,
  procs,
  onSelect,
  onClone,
  onDelete,
  cloningSlugs,
  deletingSlugs,
}: {
  fleet: FleetMember[];
  activePid: number | null;
  procs: ProcRow[];
  onSelect: (pid: number) => void;
  onClone: (member: FleetMember) => void;
  onDelete: (member: FleetMember) => void;
  cloningSlugs: Set<string>;
  deletingSlugs: Set<string>;
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
        const deleting = deletingSlugs.has(m.slug);
        // Only clones (those stamped with clone_of) get a "−". Originals are
        // protected — no delete affordance, and the backend refuses them too.
        const deletable = Boolean(m.clone_of);
        const label = m.title || m.slug;
        const style = { "--pai-color": paiColor(m.slug || m.pid) } as CSSProperties;
        return (
          <div
            key={m.pid}
            className={`fleet-tab-shell ${deletable ? "has-delete" : ""}`}
            style={style}
          >
            <button
              type="button"
              role="tab"
              aria-selected={m.pid === activePid}
              className={`fleet-tab ${m.pid === activePid ? "active" : ""} ${busy ? "busy" : ""}`}
              onClick={() => onSelect(m.pid)}
              title={m.fallback ? "Default (owner-facing) PAI" : ""}
            >
              <span className="fleet-tab-name">{label}</span>
              <span className="fleet-tab-meta">
                {busy ? "Working" : "Ready"} / PID {m.pid}
                {m.fallback ? " / Default" : ""}
              </span>
            </button>
            <div className="fleet-tab-actions">
              {deletable && (
                <button
                  type="button"
                  className="fleet-delete-button"
                  onClick={() => onDelete(m)}
                  disabled={deleting}
                  aria-label={`Delete ${label}`}
                  title="Delete this clone (permanent)"
                >
                  {deleting ? (
                    <span className="fleet-action-spinner" aria-hidden="true" />
                  ) : (
                    <span className="fleet-action-glyph" aria-hidden="true">
                      −
                    </span>
                  )}
                </button>
              )}
              <button
                type="button"
                className="fleet-clone-button"
                onClick={() => onClone(m)}
                disabled={cloning}
                aria-label={`Clone ${label}`}
                title="Clone this PAI"
              >
                {cloning ? (
                  <span className="fleet-action-spinner" aria-hidden="true" />
                ) : (
                  <span className="fleet-clone-plus fleet-action-glyph" aria-hidden="true">
                    +
                  </span>
                )}
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
