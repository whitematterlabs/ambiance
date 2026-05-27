export interface WorkingStep {
  verb: string;
  detail?: string;
}

export function humanizeStep(reason: string): WorkingStep {
  const trimmed = reason.trim();
  if (!trimmed) return { verb: "Thinking…" };

  const waiting = /^waiting on (.+)$/.exec(trimmed);
  if (waiting) return { verb: "Thinking…" };

  const bash = /^bash:\s*(.+)$/.exec(trimmed);
  if (bash) return { verb: "Running", detail: bash[1] };

  const shell = /^shell:\s*(.+)$/.exec(trimmed);
  if (shell) return { verb: "Sending keys", detail: shell[1] };

  return { verb: trimmed };
}
