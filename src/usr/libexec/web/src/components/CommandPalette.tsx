import { useMemo, useState } from "react";

// Mirrors the TUI's ProviderCommands command-palette entries.
const PROVIDERS: { label: string; key: string }[] = [
  { label: "Anthropic", key: "anthropic" },
  { label: "Deepseek", key: "deepseek" },
  { label: "OpenAI", key: "openai" },
  { label: "GLM (z.ai)", key: "zai" },
];

export function CommandPalette({
  provider,
  onPick,
  onClose,
}: {
  provider: string;
  onPick: (key: string) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const items = useMemo(() => {
    const q = query.toLowerCase();
    return PROVIDERS.map((p) => ({
      ...p,
      command: `Provider: ${p.label}`,
      help: p.key === provider ? "active" : "switch on next turn",
    })).filter((p) => p.command.toLowerCase().includes(q));
  }, [query, provider]);

  return (
    <div className="palette-overlay" onClick={onClose}>
      <div className="palette" onClick={(e) => e.stopPropagation()}>
        <input
          className="palette-input"
          placeholder="Type a command…"
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && items.length) onPick(items[0].key);
            if (e.key === "Escape") onClose();
          }}
        />
        <div className="palette-list">
          {items.map((p) => (
            <button key={p.key} className="palette-item" onClick={() => onPick(p.key)}>
              <span className="palette-cmd">{p.command}</span>
              <span className="palette-help">{p.help}</span>
            </button>
          ))}
          {!items.length && <div className="palette-empty">no matches</div>}
        </div>
      </div>
    </div>
  );
}
