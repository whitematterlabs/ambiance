import type { SendCapability, SendMode } from "../types";

const MODES: { mode: SendMode; label: string; hint: string }[] = [
  { mode: "off", label: "Off", hint: "Draft only — nothing is queued or sent" },
  { mode: "approve", label: "Approve", hint: "Proposes a send; you decide in the approval tray" },
  { mode: "auto", label: "Auto", hint: "Sends autonomously, no approval" },
];

// Owner control for the tri-state send capabilities. One segmented row per
// mounted channel; the modal approval tray still handles individual sends in
// `approve` mode — this only sets which mode a channel runs in. Channels arrive
// pre-filtered by the backend (only ones a PAI can actually send on), so an
// empty list means "no send-capable channel is mounted" and the block hides.
export function SendPermissions({
  capabilities,
  onSetMode,
}: {
  capabilities: SendCapability[];
  onSetMode: (flag: string, mode: SendMode) => void;
}) {
  if (capabilities.length === 0) return null;

  return (
    <div className="sys-block send-perms">
      <div className="sys-head">
        <span>Send permissions</span>
      </div>
      <div className="send-perms-rows">
        {capabilities.map((cap) => (
          <div key={cap.flag} className="send-perm-row">
            <span className="send-perm-channel">{cap.channel}</span>
            <div
              className="segmented send-perm-modes"
              role="radiogroup"
              aria-label={`${cap.channel} send permission`}
            >
              {MODES.map((m) => (
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
        ))}
      </div>
    </div>
  );
}
