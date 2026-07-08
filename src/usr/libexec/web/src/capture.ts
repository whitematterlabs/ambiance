// Capture gates (Cowork facets, Notetaker) are two-state no/yes and
// owner-facing enough to live as header toggles rather than perms-panel rows.
// One place for the flag set + button copy so the header, mobile sheet, and
// App agree.
//
// Cowork is three independent facets — window/tab focus, clipboard, file
// activity — each its own capability flag. The header pill toggles the group;
// the dropdown holds the per-facet switches.
export const COWORK_FLAGS = new Set([
  "cowork_window",
  "cowork_clipboard",
  "cowork_files",
]);

export const CAPTURE_FLAGS = new Set([...COWORK_FLAGS, "notetaker"]);

export const CAPTURE_COPY: Record<
  string,
  { name: string; blurb: string; onHint: string; offHint: string }
> = {
  cowork_window: {
    name: "Windows & tabs",
    blurb: "PAI sees which app, window, or browser tab you're focused on.",
    onHint: "Window tracking on — PAI sees app/window/tab focus",
    offHint: "Window tracking off — PAI can't see what you're focused on",
  },
  cowork_clipboard: {
    name: "Clipboard",
    blurb: "PAI sees what you copy (sampled when you switch apps).",
    onHint: "Clipboard watching on — PAI sees what you copy",
    offHint: "Clipboard watching off — copies aren't captured",
  },
  cowork_files: {
    name: "File activity",
    blurb: "PAI sees files changing across your home folder.",
    onHint: "File activity on — PAI sees file changes in your home folder",
    offHint: "File activity off — file changes aren't captured",
  },
  notetaker: {
    name: "Notes",
    blurb: "PAI records and transcribes your calls — only when you ask it to.",
    onHint:
      "Notes mode on — PAI may record + transcribe calls when you ask (requires system-audio permission)",
    offHint: "Notes mode off — call recording disabled",
  },
};

// Copy for the header pill, which toggles all cowork facets as a group.
export const COWORK_PILL = {
  name: "Cowork",
  onHint:
    "Cowork on — click to turn all ambient capture (windows, clipboard, files) off; per-facet switches are in the dropdown",
  offHint:
    "Cowork off — click to enable window, clipboard + file capture; per-facet switches are in the dropdown",
};
