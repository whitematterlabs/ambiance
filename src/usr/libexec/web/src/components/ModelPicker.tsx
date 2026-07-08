import { useEffect, useMemo, useState } from "react";
import * as api from "../api";
import type { ModelRow, ModelsState } from "../types";

// Per-PAI provider/model picker. Reached from the chat-head "Model" button and
// cmd+k (it replaced the old CommandPalette, whose only commands were provider
// rows wired to a dead file). Key status is found/missing only — the server
// never sends key material.
export function ModelPicker({
  pai,
  onClose,
  onStatus,
  onSwitched,
}: {
  pai: string;
  onClose: () => void;
  onStatus: (text: string) => void;
  onSwitched: () => void;
}) {
  const [data, setData] = useState<ModelsState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  // Row awaiting input: a key for `${provider}/${model}` rows, or the custom row.
  const [expanded, setExpanded] = useState<string | null>(null);
  const [keyInput, setKeyInput] = useState("");
  const [customModel, setCustomModel] = useState("");
  const [busy, setBusy] = useState(false);

  const load = () => {
    api
      .getModels(pai)
      .then(setData)
      .catch((e) => setError(String(e?.message ?? e)));
  };
  useEffect(load, [pai]);

  const rows = useMemo(() => {
    const q = query.toLowerCase();
    return (data?.rows ?? []).filter(
      (r) => !q || r.label.toLowerCase().includes(q) || r.model.toLowerCase().includes(q),
    );
  }, [data, query]);

  const rowId = (r: ModelRow) => `${r.provider}/${r.model}`;
  const isActive = (r: ModelRow) =>
    data?.current?.provider === r.provider && data?.current?.model === r.model;

  const apply = async (provider: string, model: string, label: string, key?: string) => {
    setBusy(true);
    setError(null);
    try {
      if (key) await api.setApiKey(provider, key);
      await api.setModel(pai, provider, model);
      onStatus(`${pai}: switched to ${label} — takes effect next turn`);
      onSwitched();
      onClose();
    } catch (e: any) {
      setError(String(e?.message ?? e));
      setBusy(false);
    }
  };

  const pick = (r: ModelRow) => {
    if (busy) return;
    if (r.key_status === "found") {
      void apply(r.provider, r.model, r.label);
    } else {
      setKeyInput("");
      setExpanded(expanded === rowId(r) ? null : rowId(r));
    }
  };

  const keyEnv = (provider: string) => data?.providers[provider]?.api_key_env ?? "API key";
  const customNeedsKey = data?.providers["openrouter"]?.key_status === "missing";

  return (
    <div className="palette-overlay" onClick={onClose}>
      <div className="palette" onClick={(e) => e.stopPropagation()}>
        <div className="picker-title">
          Model — {pai}
          {data?.current && <span className="picker-current">{data.current.model}</span>}
        </div>
        <input
          className="palette-input"
          placeholder="Filter models…"
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") onClose();
          }}
        />
        <div className="palette-list">
          {rows.map((r) => (
            <div key={rowId(r)}>
              <button className="palette-item" disabled={busy} onClick={() => pick(r)}>
                <span className="palette-cmd">
                  {r.label}
                  {r.tag && <span className="model-tag">{r.tag}</span>}
                </span>
                <span className="palette-help">
                  {isActive(r) ? (
                    <span className="model-active">● active</span>
                  ) : r.key_status === "found" ? (
                    "key found"
                  ) : (
                    <span className="model-need-key">need key</span>
                  )}
                </span>
              </button>
              {expanded === rowId(r) && (
                <form
                  className="model-key-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (keyInput.trim()) void apply(r.provider, r.model, r.label, keyInput);
                  }}
                >
                  <input
                    className="palette-input"
                    type="password"
                    placeholder={`${keyEnv(r.provider)}…`}
                    autoFocus
                    value={keyInput}
                    onChange={(e) => setKeyInput(e.target.value)}
                  />
                  <button type="submit" className="head-action" disabled={busy || !keyInput.trim()}>
                    Save & switch
                  </button>
                </form>
              )}
            </div>
          ))}
          {data && (
            <div>
              <button
                className="palette-item"
                disabled={busy}
                onClick={() => {
                  setKeyInput("");
                  setExpanded(expanded === "custom" ? null : "custom");
                }}
              >
                <span className="palette-cmd">OpenRouter · custom model…</span>
                <span className="palette-help">
                  {customNeedsKey ? <span className="model-need-key">need key</span> : "any slug"}
                </span>
              </button>
              {expanded === "custom" && (
                <form
                  className="model-key-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    const model = customModel.trim();
                    if (!model) return;
                    void apply(
                      "openrouter",
                      model,
                      model,
                      customNeedsKey ? keyInput : undefined,
                    );
                  }}
                >
                  <input
                    className="palette-input"
                    placeholder="vendor/model — e.g. moonshotai/kimi-k2:free"
                    autoFocus
                    value={customModel}
                    onChange={(e) => setCustomModel(e.target.value)}
                  />
                  {customNeedsKey && (
                    <input
                      className="palette-input"
                      type="password"
                      placeholder="OPENROUTER_API_KEY…"
                      value={keyInput}
                      onChange={(e) => setKeyInput(e.target.value)}
                    />
                  )}
                  <button
                    type="submit"
                    className="head-action"
                    disabled={busy || !customModel.trim() || (customNeedsKey && !keyInput.trim())}
                  >
                    {customNeedsKey ? "Save & switch" : "Switch"}
                  </button>
                </form>
              )}
            </div>
          )}
          {data && !rows.length && <div className="palette-empty">no matches</div>}
          {!data && !error && <div className="palette-empty">loading…</div>}
          {error && <div className="palette-empty model-error">{error}</div>}
        </div>
      </div>
    </div>
  );
}
