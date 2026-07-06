import { useEffect, useLayoutEffect, useRef, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { authHeaders, withTokenParam } from "../auth";
import type { ProcRow, ShellEntry, ThreadMessage } from "../types";
import type { CommandGroup } from "../commands";
import { WorkingIndicator } from "./WorkingIndicator";
import { elapsedSecs } from "../status";

// PAI attaches files by embedding an absolute on-disk path in its reply as
// markdown: `![caption](/abs/path)`. The browser can only reach files the SPA
// shipped, so any such path is routed through the auth-gated `/api/asset` route
// (token rides as a query param since <img>/fetch here can't set an
// Authorization header). Images render inline; text/markdown files are fetched
// and rendered so the owner sees the content without PAI copying it into the
// reply. Everything else — remote URLs, plain links — falls through untouched.
const IMAGE_EXTS = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"]);
const MARKDOWN_EXTS = new Set([".md", ".markdown"]);

function isLocalPath(url?: string): boolean {
  return !!url && url.startsWith("/") && !url.startsWith("/api/");
}

function extOf(path: string): string {
  const base = path.split("/").pop() ?? "";
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(dot).toLowerCase() : "";
}

function fileName(path: string): string {
  return path.split("/").pop() || path;
}

function assetUrl(path: string): string {
  return withTokenParam(`/api/asset?abs=${encodeURIComponent(path)}`);
}

// Fetch an attached text/markdown file and render it inline — the content lives
// in the file, never copied into the thread. Markdown is rendered; anything
// else shows as a scrollable monospace block. A header names the file and links
// to the raw bytes.
function FileEmbed({ path, caption }: { path: string; caption?: string }) {
  const [text, setText] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    setText(null);
    setFailed(false);
    fetch(assetUrl(path), { headers: authHeaders() })
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(String(r.status)))))
      .then((t) => alive && setText(t))
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, [path]);

  const name = caption?.trim() || fileName(path);
  const isMarkdown = MARKDOWN_EXTS.has(extOf(path));
  return (
    <div className="msg-attach">
      <div className="msg-attach-head">
        <span className="msg-attach-name">{name}</span>
        <a className="msg-attach-open" href={assetUrl(path)} target="_blank" rel="noreferrer">
          open
        </a>
      </div>
      <div className="msg-attach-body">
        {failed ? (
          <span className="msg-attach-note">Couldn't load {fileName(path)}</span>
        ) : text === null ? (
          <span className="msg-attach-note">Loading…</span>
        ) : isMarkdown ? (
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
        ) : (
          <pre className="msg-attach-pre">
            <code>{text}</code>
          </pre>
        )}
      </div>
    </div>
  );
}

// Custom renderers for the markdown attach convention. An `![](…)` embed to a
// local image shows inline; to a local text file, its rendered content; a plain
// `[…](…)` link to a local file opens the raw asset in a new tab.
const MD_COMPONENTS: Components = {
  img({ src, alt }) {
    const url = typeof src === "string" ? src : "";
    if (!isLocalPath(url)) return <img src={url} alt={alt ?? ""} />;
    if (IMAGE_EXTS.has(extOf(url)))
      return <img className="msg-attach-img" src={assetUrl(url)} alt={alt ?? ""} />;
    return <FileEmbed path={url} caption={alt} />;
  },
  a({ href, children }) {
    if (!isLocalPath(href))
      return (
        <a href={href} target="_blank" rel="noreferrer">
          {children}
        </a>
      );
    return (
      <a className="msg-attach-link" href={assetUrl(href!)} target="_blank" rel="noreferrer">
        {children}
      </a>
    );
  },
};

const STICKY_BOTTOM_PX = 72;

// Speaker → style class, matching widgets._style_message.
function senderClass(sender: string): string {
  const s = sender.toLowerCase();
  if (s === "me") return "msg-me";
  if (s === "pai") return "msg-pai";
  if (s.startsWith("[kernel")) return "msg-kernel";
  return "msg-other";
}

// Interim narration the kernel mirrors into the thread (llm.py prefixes each
// line with `» ` to mark it as thinking rather than a final reply). These get
// folded into a collapsible group so the thread reads as clean replies.
function isThinking(m: ThreadMessage): boolean {
  return !m.raw && m.sender.toLowerCase() === "pai" && m.body.trimStart().startsWith("» ");
}

function stripMarker(body: string): string {
  return body.replace(/^\s*»\s+/, "");
}

export function ChatPane({
  messages,
  shell,
  commands,
  threadKey,
  busy,
  clearMarker,
}: {
  messages: ThreadMessage[];
  shell: ShellEntry[];
  commands: CommandGroup[];
  threadKey: number | null;
  busy: ProcRow["busy"];
  clearMarker: string | null;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const previousThreadKey = useRef(threadKey);
  // Per-group override of the collapsed default, keyed by the group's first
  // message index (stable: the thread is append-only). Reset on thread switch.
  const [openGroups, setOpenGroups] = useState<Record<number, boolean>>({});
  // Per-command override of the collapsed default (default: open while running,
  // collapsed once finished), keyed by the group's stable id.
  const [openCmds, setOpenCmds] = useState<Record<number, boolean>>({});
  // Per-run override for a coalesced group of consecutive commands ("Ran N
  // tools"), keyed by the run's first command id (stable).
  const [openRuns, setOpenRuns] = useState<Record<number, boolean>>({});
  const previousOpenThreadKey = useRef(threadKey);
  if (previousOpenThreadKey.current !== threadKey) {
    previousOpenThreadKey.current = threadKey;
    if (Object.keys(openGroups).length) setOpenGroups({});
    if (Object.keys(openCmds).length) setOpenCmds({});
    if (Object.keys(openRuns).length) setOpenRuns({});
  }
  const shellSlots = Array.from({ length: messages.length + 1 }, () => [] as ShellSlot[]);
  shell.forEach((entry, index) => {
    const rawSlot = entry.afterMessageIndex ?? messages.length;
    const slot = Math.min(Math.max(rawSlot, 0), messages.length);
    shellSlots[slot].push({ entry, index });
  });
  const commandSlots = Array.from({ length: messages.length + 1 }, () => [] as CommandGroup[]);
  commands.forEach((g) => {
    const slot = Math.min(Math.max(g.afterMessageIndex, 0), messages.length);
    commandSlots[slot].push(g);
  });

  // Flatten messages + shell into ordered render blocks, folding runs of
  // consecutive thinking narration into one collapsible group. A shell feed
  // between narration lines closes the current group so ordering is preserved.
  type Block =
    | { kind: "shell"; key: string; items: ShellSlot[] }
    | { kind: "command"; key: string; group: CommandGroup }
    | { kind: "commandrun"; key: string; groups: CommandGroup[] }
    | { kind: "msg"; key: string; m: ThreadMessage }
    | { kind: "think"; key: string; start: number; items: ThinkItem[] };
  const blocks: Block[] = [];
  let pending: ThinkItem[] = [];
  const flush = () => {
    if (pending.length) {
      blocks.push({ kind: "think", key: `t${pending[0].i}`, start: pending[0].i, items: pending });
      pending = [];
    }
  };
  // Emit the shell feed then any PAI command cards anchored after message `slot`.
  const emitSlot = (slot: number) => {
    if (shellSlots[slot].length > 0) {
      flush();
      blocks.push({ kind: "shell", key: `s${slot}`, items: shellSlots[slot] });
    }
    for (const g of commandSlots[slot]) {
      flush();
      blocks.push({ kind: "command", key: `c${g.id}`, group: g });
    }
  };
  emitSlot(0);
  messages.forEach((m, i) => {
    if (isThinking(m)) {
      pending.push({ m, i });
    } else {
      flush();
      blocks.push({ kind: "msg", key: `m${i}`, m });
    }
    emitSlot(i + 1);
  });
  flush();

  // Fold runs of consecutive command cards into one collapsible "Ran N tools"
  // group so a busy turn doesn't stack a wall of one-liners in the transcript.
  // A single command stays a bare card (no point wrapping one).
  const renderBlocks: Block[] = [];
  for (const b of blocks) {
    const last = renderBlocks[renderBlocks.length - 1];
    if (b.kind === "command" && last && last.kind === "commandrun") {
      last.groups.push(b.group);
    } else if (b.kind === "command") {
      renderBlocks.push({ kind: "commandrun", key: `cr${b.group.id}`, groups: [b.group] });
    } else {
      renderBlocks.push(b);
    }
  }

  // The gradient avatar shows once at the start of each PAI run; continued PAI
  // turns (and anything after a break — you, kernel, thinking, shell) get a
  // spacer instead, matching the launch-site widget.
  const avatarFor: Record<string, boolean> = {};
  {
    let prevPai = false;
    for (const b of blocks) {
      const isPaiReply =
        b.kind === "msg" &&
        !b.m.raw &&
        b.m.body.trim() !== "" &&
        !b.m.body.trimStart().startsWith("» ") &&
        b.m.sender.toLowerCase() === "pai";
      if (isPaiReply) {
        avatarFor[b.key] = !prevPai;
        prevPai = true;
      } else {
        prevPai = false;
      }
    }
  }

  // While the PAI is working, keep the trailing (live) thinking group open so
  // its reasoning streams in view; it collapses on its own once a reply lands.
  const lastBlock = blocks[blocks.length - 1];
  const autoOpenStart = busy && lastBlock?.kind === "think" ? lastBlock.start : null;
  const isGroupOpen = (start: number) =>
    start in openGroups ? openGroups[start] : start === autoOpenStart;
  const toggleGroup = (start: number) =>
    setOpenGroups((prev) => ({ ...prev, [start]: !(start in prev ? prev[start] : start === autoOpenStart) }));

  // A command defaults to open while it runs (output streams in) and collapses
  // itself once it finishes; a manual toggle overrides that default thereafter.
  const cmdDefaultOpen = (g: CommandGroup) => g.exit === null;
  const isCmdOpen = (g: CommandGroup) =>
    g.id in openCmds ? openCmds[g.id] : cmdDefaultOpen(g);
  const toggleCmd = (g: CommandGroup) =>
    setOpenCmds((prev) => ({ ...prev, [g.id]: !(g.id in prev ? prev[g.id] : cmdDefaultOpen(g)) }));

  // A coalesced run stays open while any of its commands is still running (so
  // streaming output is visible), and collapses to "Ran N tools" once the turn
  // finishes; a manual toggle overrides thereafter. Keyed by the first cmd id.
  const runDefaultOpen = (groups: CommandGroup[]) =>
    !!busy && groups.some((g) => g.exit === null);
  const isRunOpen = (groups: CommandGroup[]) =>
    groups[0].id in openRuns ? openRuns[groups[0].id] : runDefaultOpen(groups);
  const toggleRun = (groups: CommandGroup[]) =>
    setOpenRuns((prev) => ({
      ...prev,
      [groups[0].id]: !(groups[0].id in prev ? prev[groups[0].id] : runDefaultOpen(groups)),
    }));

  // The running command's own card carries the spinner + streaming output, so
  // the bottom WorkingIndicator would be a redundant second "doing X" for it.
  // Keep the indicator only for non-command steps (thinking / waiting on model).
  const hasLiveCommand = !!busy && commands.some((g) => g.exit === null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;

    if (previousThreadKey.current !== threadKey) {
      previousThreadKey.current = threadKey;
      stickToBottom.current = true;
    }

    if (stickToBottom.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [messages, shell, commands, threadKey, busy?.reason, busy?.started_at]);

  function handleScroll() {
    const el = ref.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottom.current = distanceFromBottom <= STICKY_BOTTOM_PX;
  }

  return (
    <div className="chat-pane" ref={ref} onScroll={handleScroll}>
      {clearMarker && (
        <div className="clear-divider" role="separator" aria-label="context cleared">
          <span>context cleared · {clearMarker}</span>
        </div>
      )}
      {renderBlocks.map((b) => {
        if (b.kind === "shell") return <ShellFeed key={b.key} items={b.items} />;
        if (b.kind === "commandrun") {
          if (b.groups.length === 1) {
            const g = b.groups[0];
            return (
              <CommandCard
                key={b.key}
                group={g}
                live={!!busy}
                open={isCmdOpen(g)}
                onToggle={() => toggleCmd(g)}
              />
            );
          }
          return (
            <CommandRun
              key={b.key}
              groups={b.groups}
              live={!!busy}
              open={isRunOpen(b.groups)}
              onToggle={() => toggleRun(b.groups)}
              isCmdOpen={isCmdOpen}
              toggleCmd={toggleCmd}
            />
          );
        }
        if (b.kind === "msg")
          return <Message key={b.key} m={b.m} showAvatar={!!avatarFor[b.key]} />;
        if (b.kind !== "think") return null; // `command` is folded into `commandrun`
        return (
          <ThinkingGroup
            key={b.key}
            items={b.items}
            open={isGroupOpen(b.start)}
            onToggle={() => toggleGroup(b.start)}
          />
        );
      })}
      {busy && !hasLiveCommand && <WorkingIndicator busy={busy} />}
    </div>
  );
}

interface ShellSlot {
  entry: ShellEntry;
  index: number;
}

interface ThinkItem {
  m: ThreadMessage;
  i: number;
}

function ThinkingGroup({
  items,
  open,
  onToggle,
}: {
  items: ThinkItem[];
  open: boolean;
  onToggle: () => void;
}) {
  const count = items.length;
  const lastTs = items[items.length - 1].m.ts;
  return (
    <div className={`thinking-group${open ? " is-open" : ""}`}>
      <button className="thinking-toggle" onClick={onToggle} aria-expanded={open}>
        <span className="thinking-chevron" aria-hidden="true">▸</span>
        <span className="msg-avatar" aria-hidden="true" />
        <span className="thinking-label">Thinking</span>
        <span className="thinking-count">
          {count} step{count === 1 ? "" : "s"}
        </span>
        <span className="thinking-ts">{lastTs}</span>
      </button>
      {open && (
        <div className="thinking-steps">
          {items.map(({ m, i }) => (
            <div key={`t${i}`} className="msg-tool">
              {stripMarker(m.body)}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ShellFeed({ items }: { items: ShellSlot[] }) {
  return (
    <div className="shell-feed">
      {items.map(({ entry, index }) => (
        <div key={`s${index}`} className={`shell-line shell-${entry.kind}`}>
          {entry.text}
        </div>
      ))}
    </div>
  );
}

// A run of consecutive PAI shell commands folded into one collapsible group so
// a busy turn reads as "Ran N tools" instead of a stack of one-liners. Open
// while any command is still running (streaming output stays visible), then
// collapses. Expanded, it holds the individual command cards (each still
// independently foldable to its output).
function CommandRun({
  groups,
  live,
  open,
  onToggle,
  isCmdOpen,
  toggleCmd,
}: {
  groups: CommandGroup[];
  live: boolean;
  open: boolean;
  onToggle: () => void;
  isCmdOpen: (g: CommandGroup) => boolean;
  toggleCmd: (g: CommandGroup) => void;
}) {
  const running = live && groups.some((g) => g.exit === null);
  const count = groups.length;
  return (
    <div className={`command-run${open ? " is-open" : ""}${running ? " is-running" : ""}`}>
      <button className="command-run-toggle" onClick={onToggle} aria-expanded={open}>
        {running ? (
          <span className="command-card-spinner" aria-hidden="true" />
        ) : (
          <span className="command-run-icon" aria-hidden="true">
            ✓
          </span>
        )}
        <span className="command-run-label">
          {running ? "Running" : "Ran"} {count} tool{count === 1 ? "" : "s"}
        </span>
        <span className="command-run-chevron" aria-hidden="true">▸</span>
      </button>
      {open && (
        <div className="command-run-body">
          {groups.map((g) => (
            <CommandCard
              key={g.id}
              group={g}
              live={live}
              open={isCmdOpen(g)}
              onToggle={() => toggleCmd(g)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// One PAI shell command, inline in the transcript: live spinner + streaming
// output while it runs, collapsing to a single "✓ <cmd> · N lines" line when it
// finishes (always neutral — no failure surfacing). Click to expand the output.
function CommandCard({
  group,
  live,
  open,
  onToggle,
}: {
  group: CommandGroup;
  live: boolean;
  open: boolean;
  onToggle: () => void;
}) {
  const running = group.exit === null && live;
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, [running]);

  // Completed commands always read as a neutral success — no exit-code / failure
  // surfacing. Idle with no exit code recorded (kernel closed the command on a
  // boundary) is a neutral "done".
  const state = running ? "running" : group.exit === null ? "done" : "ok";
  const lineCount = group.out.length + (group.truncated ? 1 : 0);
  const summary =
    state === "running"
      ? "running…"
      : state === "done"
        ? "done"
        : lineCount === 0
          ? "no output"
          : `${lineCount} line${lineCount === 1 ? "" : "s"}`;
  const secs = running ? elapsedSecs(Math.floor(group.startedAt / 1000)) : 0;

  return (
    <div className={`command-card is-${state}${open ? " is-open" : ""}`}>
      <button className="command-card-toggle" onClick={onToggle} aria-expanded={open}>
        {running ? (
          <span className="command-card-spinner" aria-hidden="true" />
        ) : (
          <span className="command-card-icon" aria-hidden="true">
            {state === "ok" ? "✓" : "•"}
          </span>
        )}
        <code className="command-card-cmd">{group.cmd}</code>
        <span className="command-card-summary">{summary}</span>
        {running && <span className="command-card-elapsed">({secs}s)</span>}
        <span className="command-card-chevron" aria-hidden="true">▸</span>
      </button>
      {open && group.out.length > 0 && (
        <pre className="command-card-out">
          {group.out.join("\n")}
          {group.truncated ? "\n…" : ""}
        </pre>
      )}
    </div>
  );
}

function Message({ m, showAvatar }: { m: ThreadMessage; showAvatar: boolean }) {
  if (m.raw) {
    return (
      <article className="msg msg-other msg-raw">
        <div className="msg-body msg-plain">{m.body}</div>
      </article>
    );
  }
  if (m.body.trim() === "") return null;

  const s = m.sender.toLowerCase();
  const isPai = s === "pai";
  const isMe = s === "me";
  const isTool = m.body.trimStart().startsWith("» ");

  // Stray, ungrouped narration renders as a quiet tool line (no avatar/label).
  if (isTool) {
    return (
      <article className={`msg ${senderClass(m.sender)}`}>
        <div className="msg-tool">{m.body}</div>
      </article>
    );
  }

  // Only kernel / other senders are labeled; you and PAI go unlabeled.
  const label = isPai || isMe ? null : m.sender.replace(/^\[|\]$/g, "");
  return (
    <article className={`msg ${senderClass(m.sender)}`}>
      {isPai &&
        (showAvatar ? (
          <span className="msg-avatar" aria-hidden="true" />
        ) : (
          <span className="msg-avatar-spacer" aria-hidden="true" />
        ))}
      <div className="msg-col">
        {label && <span className="msg-label">{label}</span>}
        <div className="msg-body">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
            {m.body}
          </ReactMarkdown>
        </div>
        <span className="msg-ts-hover">{m.ts}</span>
      </div>
    </article>
  );
}
