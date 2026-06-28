import { useEffect, useRef } from "react";
import {
  MessageSquare,
  TerminalSquare,
  Mic,
  Users,
  Eraser,
  Activity,
  Cpu,
  Command,
  Power,
  Square,
  Library,
  ShieldHalf,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

// A one-shot welcome / capability tour: a single popup that states, in plain
// owner-facing language, everything the PAI web surface can do. Read-only — it
// touches no kernel state, just explains the controls already on screen.
const FEATURES: { icon: LucideIcon; title: string; body: React.ReactNode }[] = [
  {
    icon: MessageSquare,
    title: "Chat with your PAI",
    body: "Type a message and press Enter. Replies render as full markdown in the conversation.",
  },
  {
    icon: TerminalSquare,
    title: "Run shell commands",
    body: (
      <>
        Start a line with <code>!</code> to run a command in your PAI's home —{" "}
        <code>!ls</code>, <code>!git status</code>. Output streams inline.
      </>
    ),
  },
  {
    icon: Mic,
    title: "Talk to it",
    body: "Voice input (push-to-talk or a wake phrase) and replies read aloud. Toggle both in the header.",
  },
  {
    icon: Users,
    title: "Run a fleet",
    body: (
      <>
        Each PAI is a tab. Clone one with <strong>+</strong>, remove a clone with{" "}
        <strong>−</strong>, and click a tab to switch between them.
      </>
    ),
  },
  {
    icon: ShieldHalf,
    title: "root — the system PAI",
    body: (
      <>
        <strong>root</strong> (pid 1) is the kernelPAI. Talk to it about system matters —
        debugging, fleet state, capability requests. Day-to-day chat goes to your main PAI.
      </>
    ),
  },
  {
    icon: Library,
    title: "The Librarian",
    body: "A background PAI that consolidates the fleet's memory each night and serves memory writes and lookups. It's the only writer to shared memory, so PAIs never race on it.",
  },
  {
    icon: Eraser,
    title: "Manage context",
    body: (
      <>
        <strong>Clear</strong> wipes the conversation buffer (archived, recoverable).{" "}
        <strong>Compact</strong> has the PAI summarize its own history — you just tell it
        what to keep in focus.
      </>
    ),
  },
  {
    icon: Activity,
    title: "Monitor activity",
    body: "The right rail's Activity view narrates what your PAI is doing — nudges, shell runs, output — in plain language.",
  },
  {
    icon: Cpu,
    title: "Peek under the hood",
    body: "The System tab exposes live processes, the event stream, and the raw kernel log.",
  },
  {
    icon: Command,
    title: "Switch models",
    body: (
      <>
        <kbd>⌘</kbd>+<kbd>K</kbd> opens the command palette to change the provider on the
        next turn.
      </>
    ),
  },
  {
    icon: Power,
    title: "Run the kernel",
    body: "The header button powers PAI's runtime on and off — everything else attaches to it.",
  },
  {
    icon: Square,
    title: "Interrupt",
    body: (
      <>
        <kbd>Esc</kbd> cancels whatever the active PAI is doing, mid-turn.
      </>
    ),
  },
];

export function WelcomeDialog({ onClose }: { onClose: () => void }) {
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="welcome-overlay" role="presentation" onClick={onClose}>
      <div
        className="welcome-card"
        role="dialog"
        aria-modal="true"
        aria-label="Welcome to PAI"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="welcome-head">
          <h2 className="welcome-title">Welcome to PAI</h2>
          <p className="welcome-sub">Your always-on AI agent. Here's everything you can do.</p>
        </header>
        <ul className="welcome-list">
          {FEATURES.map((f) => {
            const Icon = f.icon;
            return (
              <li className="welcome-item" key={f.title}>
                <span className="welcome-icon" aria-hidden="true">
                  <Icon size={16} />
                </span>
                <div className="welcome-copy">
                  <span className="welcome-item-title">{f.title}</span>
                  <span className="welcome-item-body">{f.body}</span>
                </div>
              </li>
            );
          })}
        </ul>
        <footer className="welcome-actions">
          <button type="button" className="welcome-start" ref={closeRef} onClick={onClose}>
            Get started
          </button>
        </footer>
      </div>
    </div>
  );
}
