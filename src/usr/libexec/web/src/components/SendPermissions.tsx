import type { SendCapability, SendMode } from "../types";

const MODES: { mode: SendMode; label: string; hint: string }[] = [
  { mode: "no", label: "No", hint: "Draft only — nothing is queued or sent" },
  { mode: "ask", label: "Ask", hint: "Sends normally, but queues in the approval tray for your decision" },
  { mode: "yes", label: "Yes", hint: "Sends autonomously, no approval" },
];

// Capture gates aren't sends — give their buttons honest hover copy.
const FLAG_HINTS: Record<string, Partial<Record<SendMode, string>>> = {
  cowork: {
    yes: "PAI sees window, clipboard + file activity",
    no: "No ambient capture",
  },
  notetaker: {
    yes: "PAI may record + transcribe calls when you ask (requires system-audio permission)",
    no: "Call recording disabled",
  },
};

// Owner control for the capability permissions. One segmented row per mounted
// capability; send channels are tri-state (the modal approval tray still
// handles individual sends in `ask` mode), capture gates are two-state — each
// row renders only the modes its flag allows. Rows arrive pre-filtered by the
// backend (only capabilities a PAI can actually use), so an empty list means
// nothing is mounted and the block hides.
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
        <span>Permissions</span>
      </div>
      <div className="send-perms-rows">
        {capabilities.map((cap) => (
          <div key={cap.flag} className="send-perm-row">
            <span className="send-perm-channel">{cap.channel}</span>
            <div
              className="segmented send-perm-modes"
              role="radiogroup"
              aria-label={`${cap.channel} permission`}
            >
              {MODES.filter((m) => !cap.modes || cap.modes.includes(m.mode)).map((m) => (
                <button
                  key={m.mode}
                  role="radio"
                  aria-checked={cap.mode === m.mode}
                  title={FLAG_HINTS[cap.flag]?.[m.mode] ?? m.hint}
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
