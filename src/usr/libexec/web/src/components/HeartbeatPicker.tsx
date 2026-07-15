import { useState } from "react";
import * as api from "../api";

// Per-PAI idle heartbeat editor, reached from the chat-head "Heartbeat"
// button. The interval is idle-relative — timed from the last moment the PAI
// finished working — so this reads as "wake it if it's been quiet this long".
// Same modal chrome as ModelPicker; writes via POST /api/heartbeat and lets
// the fleet SSE reconcile the optimistic status line.

const UNITS = [
  { id: "s", label: "seconds", secs: 1 },
  { id: "m", label: "minutes", secs: 60 },
  { id: "h", label: "hours", secs: 3600 },
] as const;

type UnitId = (typeof UNITS)[number]["id"];

// Split a spec-sourced value ("30m"/"2h"/bare seconds) into input + unit,
// picking the largest unit that divides evenly so 3600 shows as "1 hours"
// rather than "3600 seconds". Unknown shapes (e.g. "2d") fall back via
// seconds; junk yields the empty default.
function splitValue(value: string | number | null | undefined): {
  amount: string;
  unit: UnitId;
} {
  if (value === null || value === undefined) return { amount: "", unit: "m" };
  let secs: number | null = null;
  if (typeof value === "number") {
    secs = value;
  } else {
    const m = /^(\d+)([smhd])$/.exec(value.trim().toLowerCase());
    if (m) {
      const mult = { s: 1, m: 60, h: 3600, d: 86400 }[m[2] as "s" | "m" | "h" | "d"];
      secs = Number(m[1]) * mult;
    }
  }
  if (secs === null || !Number.isFinite(secs) || secs <= 0) {
    return { amount: "", unit: "m" };
  }
  for (const u of [...UNITS].reverse()) {
    if (secs % u.secs === 0) return { amount: String(secs / u.secs), unit: u.id };
  }
  return { amount: String(secs), unit: "s" };
}

export function HeartbeatPicker({
  pai,
  current,
  onClose,
  onStatus,
}: {
  pai: string;
  current: string | number | null | undefined;
  onClose: () => void;
  onStatus: (text: string) => void;
}) {
  const initial = splitValue(current);
  const [amount, setAmount] = useState(initial.amount);
  const [unit, setUnit] = useState<UnitId>(initial.unit);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const unitSecs = UNITS.find((u) => u.id === unit)!.secs;
  const n = Number(amount);
  const totalSecs = Number.isInteger(n) && n > 0 ? n * unitSecs : null;

  const save = async () => {
    if (totalSecs === null) {
      setError("Enter a whole number of seconds/minutes/hours.");
      return;
    }
    if (totalSecs < 60) {
      setError("Heartbeat must be at least 60 seconds.");
      return;
    }
    setBusy(true);
    setError(null);
    const value = `${n}${unit}`;
    try {
      await api.setHeartbeat(pai, value);
      onStatus(`${pai}: heartbeat set to ${value} — resets on every turn`);
      onClose();
    } catch (e: any) {
      setError(String(e?.message ?? e));
      setBusy(false);
    }
  };

  const turnOff = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.setHeartbeat(pai, null);
      onStatus(`${pai}: heartbeat off`);
      onClose();
    } catch (e: any) {
      setError(String(e?.message ?? e));
      setBusy(false);
    }
  };

  return (
    <div className="palette-overlay" onClick={onClose}>
      <div className="palette" onClick={(e) => e.stopPropagation()}>
        <div className="picker-title">
          Heartbeat — {pai}
          {current != null && <span className="picker-current">{String(current)}</span>}
        </div>
        <form
          className="heartbeat-form"
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          <p className="heartbeat-hint">
            Wake this PAI after it has been idle for the given interval. The
            clock resets every time it finishes a turn.
          </p>
          <div className="heartbeat-row">
            <input
              className="schedule-input heartbeat-amount"
              type="number"
              min={1}
              placeholder="e.g. 30"
              autoFocus
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") onClose();
              }}
            />
            <select
              className="schedule-input heartbeat-unit"
              value={unit}
              onChange={(e) => setUnit(e.target.value as UnitId)}
            >
              {UNITS.map((u) => (
                <option key={u.id} value={u.id}>
                  {u.label}
                </option>
              ))}
            </select>
          </div>
          {error && <div className="heartbeat-error">{error}</div>}
          <div className="heartbeat-actions">
            <button
              type="button"
              className="head-action"
              disabled={busy || current == null}
              onClick={() => void turnOff()}
            >
              Off
            </button>
            <button
              type="submit"
              className="head-action primary"
              disabled={busy || !amount.trim()}
            >
              Save
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
