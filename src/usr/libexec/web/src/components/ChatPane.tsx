import { useEffect, useRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ShellEntry, ThreadMessage } from "../types";

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
}: {
  messages: ThreadMessage[];
  shell: ShellEntry[];
}) {
  const ref = useRef<HTMLDivElement>(null);
  // Auto-scroll to newest, like the TUI's scroll_end.
  useEffect(() => {
    const el = ref.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, shell]);

  return (
    <div className="chat-pane" ref={ref}>
      {messages.length === 0 && shell.length === 0 && (
        <div className="chat-empty">No messages yet for this PAI.</div>
      )}
      {messages.map((m, i) => (
        <Message key={i} m={m} />
      ))}
      {shell.length > 0 && (
        <div className="shell-feed">
          {shell.map((e, i) => (
            <div key={`s${i}`} className={`shell-line shell-${e.kind}`}>
              {e.text}
            </div>
          ))}
        </div>
      )}
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
