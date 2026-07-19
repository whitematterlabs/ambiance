import { useEffect, useState } from "react";
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

// Flags that carry an editable allowlist, with the dialog's per-channel copy.
const ALLOWLIST_COPY: Record<string, { hint: string; placeholder: string }> = {
  bash_exec: {
    hint: "Command prefixes. A command runs without asking when every segment matches a rule.",
    placeholder: "e.g. git status",
  },
  imessage_send: {
    hint: "Phone numbers, email handles, or group chat ids. Sends to these go out without asking.",
    placeholder: "e.g. +15551234567",
  },
  whatsapp_send: {
    hint: "Phone numbers or JIDs. Sends to these go out without asking.",
    placeholder: "e.g. +15551234567",
  },
  email_send: {
    hint: "Addresses or *@domain.com. Every recipient (to+cc+bcc) must match; replies always ask.",
    placeholder: "e.g. *@corp.com",
  },
};

// Owner control for the send-channel permissions. One segmented tri-state row
// per mounted channel (the modal approval tray still handles individual sends
// in `ask` mode). Capture gates (cowork/notetaker) live as header/mobile-sheet
// toggles, not here — App filters them out before this renders. Rows arrive
// pre-filtered by the backend (only capabilities a PAI can actually use), so
// an empty list means nothing is mounted and the block hides.
//
// Rows that carry an allowlist (bash_exec: command prefix rules; the send
// channels: recipient rules) show an "Allowed (n)" affordance in `ask` mode
// that opens an editor dialog. Edits go through onAllowlistChange and
// reconcile via the send_capabilities rebroadcast — the dialog reads its
// rules live off the `capabilities` prop, so it never holds stale state.
export function SendPermissions({
  capabilities,
  onSetMode,
  onAllowlistChange,
}: {
  capabilities: SendCapability[];
  onSetMode: (flag: string, mode: SendMode) => void;
  onAllowlistChange?: (flag: string, change: { add?: string; remove?: string }) => void;
}) {
  const [openFlag, setOpenFlag] = useState<string | null>(null);

  if (capabilities.length === 0) return null;

  const open = openFlag
    ? capabilities.find((c) => c.flag === openFlag) ?? null
    : null;

  return (
    <div className="sys-block send-perms">
      <div className="sys-head">
        <span>Permissions</span>
      </div>
      <div className="send-perms-rows">
        {capabilities.map((cap) => {
          const isBash = cap.flag === "bash_exec";
          const modes = isBash ? BASH_MODES : MODES;
          const editable =
            cap.mode === "ask" &&
            onAllowlistChange !== undefined &&
            cap.flag in ALLOWLIST_COPY;
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
                    title={`Edit what ${cap.channel} allows without asking`}
                    onClick={() => setOpenFlag(cap.flag)}
                  >
                    Allowed ({(cap.allowlist ?? []).length})
                  </button>
                </div>
              )}
            </div>
          );
        })}
      </div>
      {open && onAllowlistChange && (
        <AllowlistDialog
          cap={open}
          onChange={(change) => onAllowlistChange(open.flag, change)}
          onClose={() => setOpenFlag(null)}
        />
      )}
    </div>
  );
}

// Editor dialog for one capability's allowlist. Same overlay/card/ESC shell
// as ConfirmDialog; closing never discards anything — every add/remove is
// applied immediately and reconciles via rebroadcast.
function AllowlistDialog({
  cap,
  onChange,
  onClose,
}: {
  cap: SendCapability;
  onChange: (change: { add?: string; remove?: string }) => void;
  onClose: () => void;
}) {
  const [newRule, setNewRule] = useState("");
  const rules = cap.allowlist ?? [];
  const copy = ALLOWLIST_COPY[cap.flag];

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const addRule = () => {
    const rule = newRule.trim();
    if (!rule) return;
    onChange({ add: rule });
    setNewRule("");
  };

  return (
    <div className="confirm-overlay" role="presentation" onClick={onClose}>
      <div
        className="confirm-card allowlist-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={`${cap.channel} allowlist`}
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="confirm-title">Always allowed — {cap.channel}</h2>
        {copy && <p className="confirm-copy allowlist-hint">{copy.hint}</p>}
        <div className="bash-allowlist-body allowlist-dialog-body">
          {rules.length === 0 && (
            <div className="allowlist-empty">
              Nothing allowlisted yet — everything asks.
            </div>
          )}
          {rules.map((rule) => (
            <div key={rule} className="bash-allowlist-rule">
              <code>{rule}</code>
              <button
                type="button"
                className="bash-allowlist-remove"
                title={`Remove "${rule}"`}
                onClick={() => onChange({ remove: rule })}
              >
                ×
              </button>
            </div>
          ))}
          <div className="bash-allowlist-add">
            <input
              type="text"
              placeholder={copy?.placeholder ?? "Add rule"}
              value={newRule}
              autoFocus
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
        <div className="confirm-actions">
          <button type="button" className="confirm-cancel" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
