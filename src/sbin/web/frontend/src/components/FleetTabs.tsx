import type { FleetMember } from "../types";

export function FleetTabs({
  fleet,
  activePid,
  onSelect,
}: {
  fleet: FleetMember[];
  activePid: number | null;
  onSelect: (pid: number) => void;
}) {
  if (!fleet.length) {
    return <div className="tabs empty">no running PAIs — start the kernel</div>;
  }
  return (
    <div className="tabs" role="tablist">
      {fleet.map((m) => (
        <button
          key={m.pid}
          role="tab"
          aria-selected={m.pid === activePid}
          className={`tab ${m.pid === activePid ? "active" : ""}`}
          onClick={() => onSelect(m.pid)}
          title={m.fallback ? "fallback (owner-facing) PAI" : ""}
        >
          {m.slug} <span className="tab-pid">#{m.pid}</span>
          {m.fallback && <span className="tab-fallback">★</span>}
        </button>
      ))}
    </div>
  );
}
