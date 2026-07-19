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

const ALLOWLIST_PLACEHOLDER: Record<string, string> = {
  bash_exec: "Add prefix (e.g. git status)",
  imessage_send: "Add handle (e.g. +15551234567)",
  whatsapp_send: "Add phone or JID (e.g. +15551234567)",
  email_send: "Add address (e.g. *@corp.com)",
};

// Owner control for the send-channel permissions. One segmented tri-state row
// per mounted channel (the modal approval tray still handles individual sends
// in `ask` mode). Capture gates (cowork/notetaker) live as header/mobile-sheet
// toggles, not here — App filters them out before this renders. Rows arrive
// pre-filtered by the backend (only capabilities a PAI can actually use), so
// an empty list means nothing is mounted and the block hides.
//
// Rows that carry an `allowlist` (bash_exec: command prefix rules; the send
// channels: recipient rules) render an inline editor under the row in `ask`
// mode. Edits go through onAllowlistChange and reconcile via the
// send_capabilities rebroadcast — no local mutation.
export function SendPermissions({
  capabilities,
  onSetMode,
  onAllowlistChange,
}: {
  capabilities: SendCapability[];
  onSetMode: (flag: string, mode: SendMode) => void;
  onAllowlistChange?: (flag: string, change: { add?: string; remove?: string }) => void;
}) {
  const [newRule, setNewRule] = useState("");
  const [openFlag, setOpenFlag] = useState<string | null>(null);

  if (capabilities.length === 0) return null;

  const addRule = (flag: string) => {
    const rule = newRule.trim();
    if (!rule || !onAllowlistChange) return;
    onAllowlistChange(flag, { add: rule });
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
          const editable =
            cap.mode === "ask" &&
            onAllowlistChange !== undefined &&
            cap.flag in ALLOWLIST_PLACEHOLDER;
          const listOpen = openFlag === cap.flag;
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
              {editable && (
                <div className="bash-allowlist">
                  <button
                    type="button"
                    className="bash-allowlist-toggle"
                    onClick={() => {
                      setOpenFlag(listOpen ? null : cap.flag);
                      setNewRule("");
                    }}
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
                            onClick={() => onAllowlistChange(cap.flag, { remove: rule })}
                          >
                            ×
                          </button>
                        </div>
                      ))}
                      <div className="bash-allowlist-add">
                        <input
                          type="text"
                          placeholder={ALLOWLIST_PLACEHOLDER[cap.flag]}
                          value={newRule}
                          onChange={(e) => setNewRule(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") addRule(cap.flag);
                          }}
                        />
                        <button
                          type="button"
                          disabled={!newRule.trim()}
                          onClick={() => addRule(cap.flag)}
                        >
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
