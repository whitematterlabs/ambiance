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
      {messages.map((m, i) => (
        <Message key={i} m={m} />
      ))}
      {shell.map((e, i) => (
        <div key={`s${i}`} className={`shell-line shell-${e.kind}`}>
          {e.text}
        </div>
      ))}
    </div>
  );
}

function Message({ m }: { m: ThreadMessage }) {
  if (m.raw) {
    return <div className="msg msg-other">{m.body}</div>;
  }
  const isTool = m.body.trimStart().startsWith("» ");
  return (
    <div className={`msg ${senderClass(m.sender)}`}>
      <div className="msg-head">
        <span className="msg-ts">[{m.ts}]</span> <span className="msg-sender">{m.sender}:</span>
      </div>
      {m.body.trim() !== "" &&
        (isTool ? (
          <div className="msg-tool">{m.body}</div>
        ) : (
          <div className="msg-body">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.body}</ReactMarkdown>
          </div>
        ))}
    </div>
  );
}
