import { useState } from "react";
import type { SendCapability, SendMode } from "../types";

const MODES: { mode: SendMode; label: string; hint: string }[] = [
  { mode: "no", label: "No", hint: "Draft only — nothing is queued or sent" },
  { mode: "ask", label: "Ask", hint: "Sends normally, but queues in the approval tray for your decision" },
  { mode: "yes", label: "Yes", hint: "Sends autonomously, no approval" },
];

// The bash gate speaks commands, not sends — same tri-state, different hints.
const BASH_MODES: { mode: SendMode; label: string; hint: string }[] = [
  { mode: "no", label: "No", hint: "Every shell command is refused" },
  { mode: "ask", label: "Ask", hint: "Commands outside the allowlist pause for your approval" },
  { mode: "yes", label: "Yes", hint: "Commands run directly, no approval" },
];

// Owner control for the send-channel permissions. One segmented tri-state row
// per mounted channel (the modal approval tray still handles individual sends
// in `ask` mode). Capture gates (cowork/notetaker) live as header/mobile-sheet
// toggles, not here — App filters them out before this renders. Rows arrive
// pre-filtered by the backend (only capabilities a PAI can actually use), so
// an empty list means nothing is mounted and the block hides.
//
// The bash_exec row additionally carries the owner's allowlist (prefix rules
// the kernel gate runs without asking); in `ask` mode it renders an inline
// editor under the row. Edits go through onAllowlistChange and reconcile via
// the send_capabilities rebroadcast — no local mutation.
export function SendPermissions({
  capabilities,
  onSetMode,
  onAllowlistChange,
}: {
  capabilities: SendCapability[];
  onSetMode: (flag: string, mode: SendMode) => void;
  onAllowlistChange?: (change: { add?: string; remove?: string }) => void;
}) {
  const [newRule, setNewRule] = useState("");
  const [listOpen, setListOpen] = useState(false);

  if (capabilities.length === 0) return null;

  const addRule = () => {
    const rule = newRule.trim();
    if (!rule || !onAllowlistChange) return;
    onAllowlistChange({ add: rule });
    setNewRule("");
  };

  return (
    <div className="sys-block send-perms">
      <div className="sys-head">
        <span>Permissions</span>
      </div>
      <div className="send-perms-rows">
        {capabilities.map((cap) => {
          const isBash = cap.flag === "bash_exec";
          const modes = isBash ? BASH_MODES : MODES;
          const rules = cap.allowlist ?? [];
          return (
            <div key={cap.flag}>
              <div className="send-perm-row">
                <span className="send-perm-channel">{cap.channel}</span>
                <div
                  className="segmented send-perm-modes"
                  role="radiogroup"
                  aria-label={`${cap.channel} permission`}
                >
                  {modes.filter((m) => !cap.modes || cap.modes.includes(m.mode)).map((m) => (
                    <button
                      key={m.mode}
                      role="radio"
                      aria-checked={cap.mode === m.mode}
                      title={m.hint}
                      className={`segment ${cap.mode === m.mode ? "active" : ""}`}
                      onClick={() => {
                        if (cap.mode !== m.mode) onSetMode(cap.flag, m.mode);
                      }}
                    >
                      {m.label}
                    </button>
                  ))}
                </div>
              </div>
              {isBash && cap.mode === "ask" && onAllowlistChange && (
                <div className="bash-allowlist">
                  <button
                    type="button"
                    className="bash-allowlist-toggle"
                    onClick={() => setListOpen((v) => !v)}
                  >
                    {listOpen ? "▾" : "▸"} allowlist ({rules.length})
                  </button>
                  {listOpen && (
                    <div className="bash-allowlist-body">
                      {rules.map((rule) => (
                        <div key={rule} className="bash-allowlist-rule">
                          <code>{rule}</code>
                          <button
                            type="button"
                            className="bash-allowlist-remove"
                            title={`Remove "${rule}"`}
                            onClick={() => onAllowlistChange({ remove: rule })}
                          >
                            ×
                          </button>
                        </div>
                      ))}
                      <div className="bash-allowlist-add">
                        <input
                          type="text"
                          placeholder="Add prefix (e.g. git status)"
                          value={newRule}
                          onChange={(e) => setNewRule(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") addRule();
                          }}
                        />
                        <button type="button" disabled={!newRule.trim()} onClick={addRule}>
                          Add
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
