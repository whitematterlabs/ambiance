import type { ScheduledTask } from "../types";

// The Scheduled Tasks pane: a card (mirroring the `.conversation` chrome) with a
// header + New task button, the task list, and an empty state. All cron logic
// lives on the server — this only renders the label/next_fire it ships.

function formatNext(iso: string): string {
  // next_fire is local ISO (no tz) — `new Date` reads it as local time.
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function ScheduledView({
  tasks,
  onNew,
  onEdit,
  onDelete,
  deletingSlugs,
}: {
  tasks: ScheduledTask[];
  onNew: () => void;
  onEdit: (task: ScheduledTask) => void;
  onDelete: (task: ScheduledTask) => void;
  deletingSlugs: Set<string>;
}) {
  return (
    <section className="conversation scheduled-view">
      <header className="chat-head">
        <div className="chat-head-copy">
          <h1 className="chat-title">Scheduled Tasks</h1>
          <p className="chat-meta">Wake a PAI at a set time and give it something to do.</p>
        </div>
        <div className="chat-head-actions">
          <button className="head-action" type="button" onClick={onNew}>
            New task
          </button>
        </div>
      </header>
      <div className="scheduled-body">
        {tasks.length === 0 ? (
          <div className="scheduled-empty">
            <p className="scheduled-empty-title">No scheduled tasks yet</p>
            <p className="scheduled-empty-copy">
              Create a recurring job and a PAI will wake at the time you set and act on your
              instruction.
            </p>
            <button className="head-action" type="button" onClick={onNew}>
              Create one
            </button>
          </div>
        ) : (
          <ul className="scheduled-list">
            {tasks.map((t) => {
              const deleting = deletingSlugs.has(t.slug);
              return (
                <li key={t.slug} className="scheduled-item">
                  <div className="scheduled-item-main">
                    <div className="scheduled-when">
                      <span className="scheduled-clock" aria-hidden="true">
                        ⏰
                      </span>
                      <span className="scheduled-label">{t.label}</span>
                      {t.pai && <span className="scheduled-pai">→ {t.pai}</span>}
                      {t.source === "pai" && (
                        <span className="scheduled-source" title={`Scheduled via paicron (${t.slug})`}>
                          paicron
                        </span>
                      )}
                    </div>
                    {t.instruction && (
                      <p className="scheduled-instruction">{t.instruction}</p>
                    )}
                    <p className="scheduled-next">
                      {t.next_fire ? `Next: ${formatNext(t.next_fire)}` : "Not scheduled to fire"}
                    </p>
                  </div>
                  <div className="scheduled-item-actions">
                    <button
                      className="head-action"
                      type="button"
                      onClick={() => onEdit(t)}
                      disabled={deleting || t.repeat === "custom" || t.source === "pai"}
                      title={
                        t.source === "pai"
                          ? "Scheduled by a PAI via paicron — delete it, or ask the PAI to change it"
                          : t.repeat === "custom"
                            ? "This task uses a custom schedule and can't be edited here"
                            : "Edit this task"
                      }
                    >
                      Edit
                    </button>
                    <button
                      className="head-action danger"
                      type="button"
                      onClick={() => onDelete(t)}
                      disabled={deleting}
                    >
                      {deleting ? "Deleting…" : "Delete"}
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </section>
  );
}
