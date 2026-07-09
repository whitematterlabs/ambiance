import { useEffect, useMemo, useState } from "react";
import * as api from "../api";
import type { FleetMember, ScheduledTask } from "../types";

// Add/edit modal for a scheduled task (ModelPicker overlay pattern). Structured
// fields only — the server owns every cron string. On success it shows the
// backend's label + next_fire as a plain-English confirmation before closing.

const REPEATS: { id: string; label: string }[] = [
  { id: "once", label: "Once" },
  { id: "daily", label: "Daily" },
  { id: "weekdays", label: "Weekdays" },
  { id: "weekly", label: "Weekly" },
];

const DAYS = [
  "Sunday",
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
];

function localToday(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function formatNext(iso: string): string {
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

export function ScheduleEditor({
  task,
  fleet,
  defaultPai,
  onClose,
}: {
  // null → new task; otherwise editing an existing one.
  task: ScheduledTask | null;
  // Running fleet PAIs (the only valid targets — a stopped PAI can't be resolved).
  fleet: FleetMember[];
  defaultPai: string | null;
  onClose: () => void;
}) {
  const today = useMemo(localToday, []);
  const initialPai =
    task?.pai ||
    (defaultPai && fleet.some((f) => f.slug === defaultPai) ? defaultPai : fleet[0]?.slug || "");

  const [pai, setPai] = useState(initialPai);
  const [repeat, setRepeat] = useState<string>(
    task && task.repeat !== "custom" ? task.repeat : "daily",
  );
  const [time, setTime] = useState(task?.time || "09:00");
  const [dow, setDow] = useState<number>(task?.dow ?? 1);
  const [date, setDate] = useState(task?.date || today);
  const [instruction, setInstruction] = useState(task?.instruction || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<ScheduledTask | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const submit = async () => {
    if (busy) return;
    const body: api.ScheduleBody = {
      pai,
      repeat,
      time,
      instruction,
      ...(repeat === "weekly" ? { dow } : {}),
      ...(repeat === "once" ? { date } : {}),
    };
    setBusy(true);
    setError(null);
    try {
      const res = task
        ? await api.updateScheduled(task.slug, body)
        : await api.addScheduled(body);
      if (!res.ok || !res.task) {
        setError(res.error || "could not save the task");
        setBusy(false);
        return;
      }
      // Show the server's label + next fire as confirmation; the list also
      // reconciles via the `scheduled` SSE broadcast.
      setSaved(res.task);
    } catch (e: any) {
      setError(String(e?.message ?? e));
      setBusy(false);
    }
  };

  const canSubmit = pai && instruction.trim() && time && !busy;

  return (
    <div className="palette-overlay" role="presentation" onClick={() => !busy && onClose()}>
      <div
        className="palette schedule-editor"
        role="dialog"
        aria-modal="true"
        aria-label={task ? "Edit scheduled task" : "New scheduled task"}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="picker-title">{task ? "Edit scheduled task" : "New scheduled task"}</div>

        {saved ? (
          <div className="schedule-confirm">
            <p className="schedule-confirm-label">✓ {saved.label}</p>
            <p className="schedule-confirm-next">
              {saved.pai && <>Wakes <strong>{saved.pai}</strong>. </>}
              {saved.next_fire
                ? `Next fire ${formatNext(saved.next_fire)}.`
                : "No upcoming fire."}
            </p>
            <div className="schedule-actions">
              <button type="button" className="head-action" onClick={onClose}>
                Done
              </button>
            </div>
          </div>
        ) : (
          <form
            className="schedule-form"
            onSubmit={(e) => {
              e.preventDefault();
              if (canSubmit) void submit();
            }}
          >
            <label className="schedule-field">
              <span className="schedule-field-label">PAI</span>
              {fleet.length === 0 ? (
                <span className="schedule-hint">No running PAI to schedule against.</span>
              ) : (
                <select
                  className="schedule-input"
                  value={pai}
                  onChange={(e) => setPai(e.target.value)}
                >
                  {fleet.map((f) => (
                    <option key={f.slug} value={f.slug}>
                      {f.title || f.slug}
                    </option>
                  ))}
                </select>
              )}
            </label>

            <label className="schedule-field">
              <span className="schedule-field-label">Repeat</span>
              <select
                className="schedule-input"
                value={repeat}
                onChange={(e) => setRepeat(e.target.value)}
              >
                {REPEATS.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.label}
                  </option>
                ))}
              </select>
            </label>

            {repeat === "once" && (
              <label className="schedule-field">
                <span className="schedule-field-label">Date</span>
                <input
                  className="schedule-input"
                  type="date"
                  min={today}
                  value={date}
                  onChange={(e) => setDate(e.target.value)}
                />
              </label>
            )}

            {repeat === "weekly" && (
              <label className="schedule-field">
                <span className="schedule-field-label">Day</span>
                <select
                  className="schedule-input"
                  value={dow}
                  onChange={(e) => setDow(Number(e.target.value))}
                >
                  {DAYS.map((d, i) => (
                    <option key={i} value={i}>
                      {d}
                    </option>
                  ))}
                </select>
              </label>
            )}

            <label className="schedule-field">
              <span className="schedule-field-label">Time</span>
              <input
                className="schedule-input"
                type="time"
                value={time}
                onChange={(e) => setTime(e.target.value)}
              />
            </label>

            <label className="schedule-field">
              <span className="schedule-field-label">Instruction</span>
              <textarea
                className="schedule-input schedule-textarea"
                placeholder="e.g. summarize overnight email and flag anything urgent"
                value={instruction}
                onChange={(e) => setInstruction(e.target.value)}
                rows={3}
              />
            </label>

            {error && <div className="schedule-error">{error}</div>}

            <div className="schedule-actions">
              <button type="button" className="head-action" onClick={onClose} disabled={busy}>
                Cancel
              </button>
              <button type="submit" className="head-action primary" disabled={!canSubmit}>
                {busy ? "Saving…" : task ? "Save" : "Schedule"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
