// Top-level tab bar for the center content column. Scoped to `.chat-col` (below
// the global Header), it swaps the main pane between Chat and Scheduled Tasks.
// Architected so future views (Calendar, To-Dos) are a one-line TABS addition.

export type MainView = "chat" | "scheduled";

const TABS: { id: MainView; label: string }[] = [
  { id: "chat", label: "Chat" },
  { id: "scheduled", label: "Scheduled Tasks" },
];

export function MainTabs({
  view,
  onChange,
}: {
  view: MainView;
  onChange: (v: MainView) => void;
}) {
  return (
    <div className="main-tabs" role="tablist" aria-label="Main view">
      {TABS.map((t) => (
        <button
          key={t.id}
          type="button"
          role="tab"
          aria-selected={view === t.id}
          className={`main-tab${view === t.id ? " active" : ""}`}
          onClick={() => onChange(t.id)}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}
