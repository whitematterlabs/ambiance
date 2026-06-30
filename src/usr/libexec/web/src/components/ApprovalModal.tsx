import { useEffect, useRef, useState } from "react";
import type { PendingApproval } from "../types";

// Draft & approve: a modal overlay that pops to the foreground when a PAI under
// a send capability in `approve` mode proposes a send. Each card shows the
// recipient + the full body so the owner reads exactly what would go out, then
// approves or rejects. Modeled on ConfirmDialog (same overlay/card/focus-trap/
// ESC shell). Closing does NOT decide — items stay pending and reachable via
// the header badge. Approve/reject don't mutate local state: the hub's file
// watcher rebroadcasts the shrunken list, which is the single source of truth.
export function ApprovalModal({
  approvals,
  onApprove,
  onReject,
  onClose,
}: {
  approvals: PendingApproval[];
  onApprove: (id: string) => Promise<unknown> | void;
  onReject: (id: string, reason: string) => Promise<unknown> | void;
  onClose: () => void;
}) {
  const cardRef = useRef<HTMLDivElement>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [rejectingId, setRejectingId] = useState<string | null>(null);
  const [reason, setReason] = useState("");

  useEffect(() => {
    cardRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && busyId === null) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busyId, onClose]);

  const runApprove = async (id: string) => {
    setBusyId(id);
    try {
      await onApprove(id);
    } finally {
      setBusyId(null);
    }
  };

  const runReject = async (id: string) => {
    setBusyId(id);
    try {
      await onReject(id, reason.trim());
    } finally {
      setBusyId(null);
      setRejectingId(null);
      setReason("");
    }
  };

  const title =
    approvals.length === 1
      ? "A send needs your approval"
      : `${approvals.length} sends need your approval`;

  return (
    <div
      className="confirm-overlay"
      role="presentation"
      onClick={() => {
        if (busyId === null) onClose();
      }}
    >
      <div
        className="confirm-card approval-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Sends awaiting approval"
        tabIndex={-1}
        ref={cardRef}
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="confirm-title">{title}</h2>
        <div className="approval-list">
          {approvals.map((a) => {
            const busy = busyId === a.id;
            const rejecting = rejectingId === a.id;
            return (
              <div className="approval-card" key={a.id}>
                <div className="approval-head">
                  <span className="approval-channel">{a.channel || "send"}</span>
                  {a.recipient && (
                    <span className="approval-recipient">→ {a.recipient}</span>
                  )}
                </div>
                {a.subject && <div className="approval-subject">{a.subject}</div>}
                {a.summary && <div className="approval-summary">{a.summary}</div>}
                <pre className="approval-body">{a.body || "(empty body)"}</pre>
                {rejecting ? (
                  <div className="approval-reason-row">
                    <input
                      className="approval-reason"
                      type="text"
                      placeholder="Reason (optional)"
                      value={reason}
                      autoFocus
                      disabled={busy}
                      onChange={(e) => setReason(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") runReject(a.id);
                      }}
                    />
                    <button
                      type="button"
                      className="confirm-delete"
                      disabled={busy}
                      onClick={() => runReject(a.id)}
                    >
                      {busy ? "Rejecting…" : "Confirm"}
                    </button>
                    <button
                      type="button"
                      className="confirm-cancel"
                      disabled={busy}
                      onClick={() => {
                        setRejectingId(null);
                        setReason("");
                      }}
                    >
                      Back
                    </button>
                  </div>
                ) : (
                  <div className="approval-actions">
                    <button
                      type="button"
                      className="head-action approval-approve"
                      disabled={busy}
                      onClick={() => runApprove(a.id)}
                    >
                      {busy ? "Approving…" : "Approve"}
                    </button>
                    <button
                      type="button"
                      className="confirm-delete approval-reject"
                      disabled={busy}
                      onClick={() => {
                        setRejectingId(a.id);
                        setReason("");
                      }}
                    >
                      Reject
                    </button>
                  </div>
                )}
              </div>
            );
          })}
        </div>
        <div className="confirm-actions">
          <button
            type="button"
            className="confirm-cancel"
            disabled={busyId !== null}
            onClick={onClose}
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
