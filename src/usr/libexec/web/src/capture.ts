// Capture gates (Cowork Mode, Notetaker) are two-state no/yes and owner-facing
// enough to live as header toggles rather than perms-panel rows. One place for
// the flag set + button copy so the header, mobile sheet, and App agree.
export const CAPTURE_FLAGS = new Set(["cowork", "notetaker"]);

export const CAPTURE_COPY: Record<
  string,
  { name: string; onHint: string; offHint: string }
> = {
  cowork: {
    name: "Cowork",
    onHint: "Cowork mode on — PAI sees window, clipboard + file activity",
    offHint: "Cowork mode off — no ambient capture",
  },
  notetaker: {
    name: "Notes",
    onHint:
      "Notes mode on — PAI may record + transcribe calls when you ask (requires system-audio permission)",
    offHint: "Notes mode off — call recording disabled",
  },
};
