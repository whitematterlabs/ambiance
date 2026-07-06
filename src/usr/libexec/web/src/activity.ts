// Port of the TUI's PaiActivity.ingest (src/sbin/tui/widgets.py): turn raw
// kernel.log lines into a readable activity feed — nudges, each PAI shell
// command with its exit status, and PAI output (elided to ~2 lines).

export interface ActivityEntry {
  cls: string;
  text: string;
  pai?: string;
}

export interface ActivityState {
  inCommand: boolean;
  outLines: number;
  commandPai?: string;
}

export const initialActivity = (): ActivityState => ({
  inCommand: false,
  outLines: 0,
});

const PAI_PREFIX = /^\[pai(?::([^\]]+))?\]/;

export function ingest(
  state: ActivityState,
  line: string,
): { state: ActivityState; entries: ActivityEntry[] } {
  const out: ActivityEntry[] = [];
  const s = { ...state };

  if (line.startsWith("--- pai supervisor")) {
    out.push({ cls: "act-dim", text: line });
    s.inCommand = false;
    s.commandPai = undefined;
    return { state: s, entries: out };
  }
  if (line.startsWith("[kernel] nudge:")) {
    out.push({ cls: "act-nudge", text: "> " + line.slice("[kernel] ".length) });
    s.inCommand = false;
    s.commandPai = undefined;
    return { state: s, entries: out };
  }
  if (line.startsWith("[kernel] nudge failed")) {
    out.push({ cls: "act-dim", text: line.slice("[kernel] ".length) });
    s.inCommand = false;
    s.commandPai = undefined;
    return { state: s, entries: out };
  }
  if (line.startsWith("[kernel] nudge complete")) {
    out.push({ cls: "act-done", text: "  done." });
    s.inCommand = false;
    s.commandPai = undefined;
    return { state: s, entries: out };
  }

  const m = PAI_PREFIX.exec(line);
  if (m) {
    const pai = m[1] || "";
    const rest = line.slice(m[0].length).replace(/^ +/, "");
    const tag = pai ? `pai:${pai}` : "pai";
    if (rest.startsWith("$ ")) {
      out.push({ cls: "act-cmd", text: `[${tag}] $ ${rest.slice(2)}`, pai });
      s.inCommand = true;
      s.outLines = 0;
      s.commandPai = pai || undefined;
    } else {
      out.push({ cls: "act-pai", text: `${tag}: ${rest}`, pai });
      s.inCommand = false;
      s.commandPai = undefined;
    }
    return { state: s, entries: out };
  }

  if (s.inCommand) {
    const stripped = line.trim();
    if (stripped.startsWith("[exit")) {
      // Command finished — always a neutral "done"; no pass/fail surfacing.
      out.push({
        cls: "act-done",
        text: `    done`,
        pai: s.commandPai,
      });
      s.inCommand = false;
      s.commandPai = undefined;
    } else if (stripped === "[stderr]") {
      // skip
    } else if (s.outLines < 2) {
      const preview =
        stripped.length <= 80 ? stripped : stripped.slice(0, 77) + "…";
      out.push({ cls: "act-out", text: `    ${preview}`, pai: s.commandPai });
      s.outLines += 1;
    } else if (s.outLines === 2) {
      out.push({ cls: "act-out", text: "    …", pai: s.commandPai });
      s.outLines += 1;
    }
  }

  return { state: s, entries: out };
}
