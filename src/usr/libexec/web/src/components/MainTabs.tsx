// Top-level tab bar for the center content column. Scoped to `.chat-col` (below
// the global Header), it swaps the main pane between Chat, Scheduled Tasks, and
// one tab per PAI-authored dashboard. Dashboard views are namespaced `dash:<slug>`
// so the active-view comparison stays plain string equality (no collision with
// the fixed "chat"/"scheduled" ids). The bar scrolls horizontally when the
// dashboards overflow.

import { Plus } from "lucide-react";

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
  onNewDashboard,
}: {
  view: MainView;
  onChange: (v: MainView) => void;
  dashboards: DashboardMeta[];
  onNewDashboard: () => void;
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
      {/* "+" = new dashboard. Dashboards are PAI-authored, so the button doesn't
          open an editor — it hands the ask to root's chat (see App). */}
      <button
        type="button"
        className="main-tab main-tab-add"
        onClick={onNewDashboard}
        title="New dashboard — ask root to create one"
        aria-label="New dashboard"
      >
        <Plus size={15} aria-hidden="true" />
      </button>
    </div>
  );
}
