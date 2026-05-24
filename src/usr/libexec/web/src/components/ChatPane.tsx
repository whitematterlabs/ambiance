import { Fragment, useLayoutEffect, useRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ShellEntry, ThreadMessage } from "../types";

const STICKY_BOTTOM_PX = 72;

// Speaker → style class, matching widgets._style_message.
function senderClass(sender: string): string {
  const s = sender.toLowerCase();
  if (s === "me") return "msg-me";
  if (s === "pai") return "msg-pai";
  if (s.startsWith("[kernel")) return "msg-kernel";
  return "msg-other";
}

export function ChatPane({
  messages,
  shell,
  threadKey,
}: {
  messages: ThreadMessage[];
  shell: ShellEntry[];
  threadKey: number | null;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const previousThreadKey = useRef(threadKey);
  const shellSlots = Array.from({ length: messages.length + 1 }, () => [] as ShellSlot[]);
  shell.forEach((entry, index) => {
    const rawSlot = entry.afterMessageIndex ?? messages.length;
    const slot = Math.min(Math.max(rawSlot, 0), messages.length);
    shellSlots[slot].push({ entry, index });
  });

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
  }, [messages, shell, threadKey]);

  function handleScroll() {
    const el = ref.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottom.current = distanceFromBottom <= STICKY_BOTTOM_PX;
  }

  return (
    <div className="chat-pane" ref={ref} onScroll={handleScroll}>
      {messages.length === 0 && shell.length === 0 && (
        <div className="chat-empty">
          <span>Say hello to start this conversation.</span>
        </div>
      )}
      {shellSlots[0].length > 0 && <ShellFeed items={shellSlots[0]} />}
      {messages.map((m, i) => (
        <Fragment key={`m${i}`}>
          <Message m={m} />
          {shellSlots[i + 1].length > 0 && <ShellFeed items={shellSlots[i + 1]} />}
        </Fragment>
      ))}
    </div>
  );
}

interface ShellSlot {
  entry: ShellEntry;
  index: number;
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

function Message({ m }: { m: ThreadMessage }) {
  if (m.raw) {
    return (
      <article className="msg msg-other msg-raw">
        <div className="msg-body msg-plain">{m.body}</div>
      </article>
    );
  }
  const isTool = m.body.trimStart().startsWith("» ");
  const sender = m.sender.toLowerCase() === "me" ? "You" : m.sender;
  return (
    <article className={`msg ${senderClass(m.sender)}`}>
      <div className="msg-head">
        <span className="msg-sender">{sender}</span>
        <span className="msg-ts">{m.ts}</span>
      </div>
      {m.body.trim() !== "" &&
        (isTool ? (
          <div className="msg-tool">{m.body}</div>
        ) : (
          <div className="msg-body">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.body}</ReactMarkdown>
          </div>
        ))}
    </article>
  );
}
