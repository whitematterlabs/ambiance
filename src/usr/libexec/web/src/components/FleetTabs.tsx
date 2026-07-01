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
  onKill,
  cloningSlugs,
  deletingSlugs,
  killingSlugs,
  variant = "strip",
}: {
  fleet: FleetMember[];
  activePid: number | null;
  procs: ProcRow[];
  onSelect: (pid: number) => void;
  onClone: (member: FleetMember) => void;
  onDelete: (member: FleetMember) => void;
  onKill: (member: FleetMember) => void;
  cloningSlugs: Set<string>;
  deletingSlugs: Set<string>;
  killingSlugs: Set<string>;
  // "strip" = horizontal top bar (legacy); "rail" = vertical list in the sidebar.
  variant?: "strip" | "rail";
}) {
  const container = variant === "rail" ? "fleet-rail" : "fleet-strip";
  if (!fleet.length) {
    return <div className={`${container} empty`}>No running PAIs</div>;
  }
  const busyPids = new Set(procs.filter((r) => r.busy).map((r) => r.pid));
  // A fleet member is a subagent when its proc row is typed `subagent:<pkg>`
  // (kind:pai carrying a parent). Subagents get a kill "✕" instead of the
  // clone/delete affordances — cloning a transient task PAI is nonsensical.
  const typeByPid = new Map(procs.map((r) => [r.pid, r.type]));
  return (
    <div className={container} role="tablist">
      {fleet.map((m) => {
        const busy = busyPids.has(String(m.pid));
        const cloning = cloningSlugs.has(m.slug);
        const deleting = deletingSlugs.has(m.slug);
        const killing = killingSlugs.has(m.slug);
        const isSubagent = (typeByPid.get(String(m.pid)) ?? "").startsWith("subagent");
        // Only clones (those stamped with clone_of) get a "−". Originals are
        // protected — no delete affordance, and the backend refuses them too.
        const deletable = Boolean(m.clone_of) && !isSubagent;
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
                {busy ? "Working" : "Ready"}
                {m.fallback ? " / Default" : ""}
              </span>
            </button>
            <div className="fleet-tab-actions">
              {isSubagent ? (
                <button
                  type="button"
                  className="fleet-kill-button"
                  onClick={() => onKill(m)}
                  disabled={killing}
                  aria-label={`Kill ${label}`}
                  title="Kill this subagent (aborts its task)"
                >
                  {killing ? (
                    <span className="fleet-action-spinner" aria-hidden="true" />
                  ) : (
                    <span className="fleet-action-glyph" aria-hidden="true">
                      ✕
                    </span>
                  )}
                </button>
              ) : (
                <>
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
                </>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
