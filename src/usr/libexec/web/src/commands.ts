// Folds the raw kernel.log stream into per-command groups so a PAI's shell
// activity can render inline in the chat as foldable cards — live while the
// command runs (streaming output + spinner), collapsed to one line when it
// finishes. This is a lossier-free sibling of activity.ts `ingest`: same line
// grammar (`[pai:slug] $ cmd` → output → `[exit N]`), but it keeps the full
// output and groups it instead of eliding to two lines for the sidebar.
//
// The kernel writes one command and its output contiguously (PAIs run serially
// in the single-threaded loop), so at most one group is "open" at a time —
// matching activity.ts's single `inCommand` assumption.

export interface CommandGroup {
  id: number; // stable, monotonic — the React key and open-state key
  slug: string; // PAI slug from `[pai:slug]`; "" for a bare `[pai]`
  cmd: string;
  out: string[]; // output lines, capped at OUT_CAP
  truncated: boolean; // true once output exceeded OUT_CAP
  exit: string | null; // null while running; otherwise the exit code text
  startedAt: number; // ms epoch when the `$ cmd` line was seen
  afterMessageIndex: number; // anchor within the slug's thread (slotting)
  // Seeded from the reconnect backlog rather than a live line. Completed
  // historical groups are not rendered inline (the sidebar owns full history);
  // a still-running one is flipped live so an in-flight command survives a
  // reconnect. Set once at creation.
  historical: boolean;
}

export interface CommandState {
  groups: CommandGroup[];
  openId: number | null; // the currently-running group, or null
  outLines: number; // output lines captured for the open group
  nextId: number;
}

const OUT_CAP = 200; // per-command output cap (memory guard on huge dumps)
const GROUP_CAP = 300; // ring-buffer across all PAIs

const PAI_PREFIX = /^\[pai(?::([^\]]+))?\]/;

export const initialCommands = (): CommandState => ({
  groups: [],
  openId: null,
  outLines: 0,
  nextId: 1,
});

// A boundary marker (nudge start/end, supervisor banner) or a narration line
// closes any open command without adding to it.
function closeOpen(state: CommandState): CommandState {
  if (state.openId === null) return state;
  return { ...state, openId: null, outLines: 0 };
}

export function ingestCommand(
  state: CommandState,
  line: string,
  nowMs: number,
  anchorFor: (slug: string) => number,
  historical = false,
): CommandState {
  if (
    line.startsWith("--- pai supervisor") ||
    line.startsWith("[kernel] nudge:") ||
    line.startsWith("[kernel] nudge failed") ||
    line.startsWith("[kernel] nudge complete")
  ) {
    return closeOpen(state);
  }

  const m = PAI_PREFIX.exec(line);
  if (m) {
    const slug = m[1] || "";
    const rest = line.slice(m[0].length).replace(/^ +/, "");
    if (rest.startsWith("$ ")) {
      const group: CommandGroup = {
        id: state.nextId,
        slug,
        cmd: rest.slice(2),
        out: [],
        truncated: false,
        exit: null,
        startedAt: nowMs,
        afterMessageIndex: anchorFor(slug),
        historical,
      };
      const grown = state.groups.concat(group);
      const groups =
        grown.length > GROUP_CAP ? grown.slice(grown.length - GROUP_CAP) : grown;
      return { groups, openId: group.id, outLines: 0, nextId: state.nextId + 1 };
    }
    // `[pai:slug] » narration` (or any non-`$` PAI line) — not a command.
    return closeOpen(state);
  }

  // A bare line belongs to the open command as output or its exit status.
  if (state.openId === null) return state;
  const stripped = line.trim();
  if (stripped === "[stderr]") return state; // section marker, not output

  if (stripped.startsWith("[exit")) {
    const codeText = stripped.replace(/^\[|\]$/g, ""); // "exit N"
    const code = codeText.split(/\s+/).pop() || "?";
    return {
      ...state,
      groups: state.groups.map((g) =>
        g.id === state.openId ? { ...g, exit: code } : g,
      ),
      openId: null,
      outLines: 0,
    };
  }

  if (state.outLines >= OUT_CAP) {
    return {
      ...state,
      groups: state.groups.map((g) =>
        g.id === state.openId && !g.truncated ? { ...g, truncated: true } : g,
      ),
    };
  }
  return {
    ...state,
    outLines: state.outLines + 1,
    groups: state.groups.map((g) =>
      g.id === state.openId ? { ...g, out: g.out.concat(stripped) } : g,
    ),
  };
}

// After seeding from the reconnect backlog, the last still-open group is an
// in-flight command — flip it live so it keeps rendering and its completion
// streams in. Completed backlog groups stay historical (sidebar-only).
export function promoteOpenGroup(state: CommandState): CommandState {
  if (state.openId === null) return state;
  return {
    ...state,
    groups: state.groups.map((g) =>
      g.id === state.openId ? { ...g, historical: false } : g,
    ),
  };
}
