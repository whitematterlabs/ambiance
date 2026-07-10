// Top-level tab bar for the center content column. Scoped to `.chat-col` (below
// the global Header), it swaps the main pane between Chat, Scheduled Tasks, and
// one tab per PAI-authored dashboard. Dashboard views are namespaced `dash:<slug>`
// so the active-view comparison stays plain string equality (no collision with
// the fixed "chat"/"scheduled" ids). The bar scrolls horizontally when the
// dashboards overflow.

import type { DashboardMeta } from "../types";

export type MainView = "chat" | "scheduled" | `dash:${string}`;

export function dashView(slug: string): MainView {
  return `dash:${slug}`;
}

const BASE_TABS: { id: MainView; label: string }[] = [
  { id: "chat", label: "Chat" },
  { id: "scheduled", label: "Scheduled Tasks" },
];

export function MainTabs({
  view,
  onChange,
  dashboards,
}: {
  view: MainView;
  onChange: (v: MainView) => void;
  dashboards: DashboardMeta[];
}) {
  const tabs: { id: MainView; label: string }[] = [
    ...BASE_TABS,
    ...dashboards.map((d) => ({ id: dashView(d.slug), label: d.title })),
  ];
  return (
    <div className="main-tabs" role="tablist" aria-label="Main view">
      {tabs.map((t) => (
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
