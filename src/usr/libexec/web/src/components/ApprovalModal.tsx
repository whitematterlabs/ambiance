import { useEffect, useRef, useState } from "react";
import type { PendingApproval } from "../types";

// Draft & approve: a modal overlay that pops to the foreground when a PAI under
// a send capability in `ask` mode sends and gets queued. Shows exactly one
// pending item at a time (the oldest) with the body in an editable textarea —
// the owner reads exactly what would go out and can tweak it before deciding.
// Modeled on ConfirmDialog (same overlay/card/focus-trap/ESC shell). Closing
// does NOT decide — items stay pending and reachable via the header badge.
// Approve/reject don't mutate local state: the hub's file watcher rebroadcasts
// the shrunken list, which is the single source of truth; the modal advances to
// the next item (or closes if the queue emptied) as that list shrinks.
//
// `channel: bash` items are kernel-gated shell commands (the PAI's turn is
// blocked mid-tool-call on this decision): the body is the command itself,
// rendered monospace.
//
// "Approve & always allow" carries the exact grant in its label — the derived
// allowlist rule(s): the command itself for bash (an exact prefix rule; the
// owner broadens it later in the sidebar editor if wanted), the recipient(s)
// for sends. No label, no button: a record with no honest rule (e.g. an email
// reply, whose recipients live on the parent message) only offers plain
// Approve.
export function ApprovalModal({
  approvals,
  onApprove,
  onReject,
  onAlwaysAllow,
  onClose,
}: {
  approvals: PendingApproval[];
  onApprove: (id: string, body: string) => Promise<unknown> | void;
  onReject: (id: string, reason: string) => Promise<unknown> | void;
  onAlwaysAllow?: (id: string, body: string) => Promise<unknown> | void;
  onClose: () => void;
}) {
  const cardRef = useRef<HTMLDivElement>(null);
  const [busy, setBusy] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [body, setBody] = useState("");

  const current = approvals[0] ?? null;
  const currentId = current?.id ?? null;
  const isBash = current?.channel === "bash";

  // Reset the editable body + reject state whenever the front-of-queue item
  // changes (a decision resolved, or a new item overtook it).
  useEffect(() => {
    setBody(current?.body ?? "");
    setRejecting(false);
    setReason("");
  }, [currentId]);

  // Focus the card once on mount only. The ESC listener below re-binds on every
  // App re-render (onClose is an inline closure), and while the PAI streams
  // those re-renders are constant — re-focusing there would yank the caret out
  // of the textarea mid-typing.
  useEffect(() => {
    cardRef.current?.focus();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  if (!current) return null;

  const runApprove = async () => {
    setBusy(true);
    try {
      await onApprove(current.id, body);
    } finally {
      setBusy(false);
    }
  };

  const runReject = async () => {
    setBusy(true);
    try {
      await onReject(current.id, reason.trim());
    } finally {
      setBusy(false);
    }
  };

  const runAlwaysAllow = async () => {
    if (!onAlwaysAllow) return;
    setBusy(true);
    try {
      await onAlwaysAllow(current.id, body);
    } finally {
      setBusy(false);
    }
  };

  // What "always allow" grants, shown verbatim (truncated for the label). For
  // bash the rule is the live (possibly edited) command; for sends the server
  // derived it from the thread/draft and it isn't editable here.
  const allowItem = isBash
    ? body.trim().split("\n")[0] ?? ""
    : (current.allow_rules ?? []).join(", ");
  const allowLabel =
    allowItem.length > 36 ? `${allowItem.slice(0, 36)}…` : allowItem;
  const canAlwaysAllow = Boolean(onAlwaysAllow) && allowItem.trim().length > 0;

  const noun = isBash ? "command" : "send";
  const title =
    approvals.length === 1
      ? `A ${noun} needs your approval`
      : `A ${noun} needs your approval (${approvals.length} queued)`;

  return (
    <div
      className="confirm-overlay"
      role="presentation"
      onClick={() => {
        if (!busy) onClose();
      }}
    >
      <div
        className="confirm-card approval-modal"
        role="dialog"
        aria-modal="true"
        aria-label={`${noun} awaiting approval`}
        tabIndex={-1}
        ref={cardRef}
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="confirm-title">{title}</h2>
        <div className="approval-card" key={current.id}>
          <div className="approval-head">
            <span className="approval-channel">{current.channel || "send"}</span>
            {isBash ? (
              current.created_by && (
                <span className="approval-recipient">from {current.created_by}</span>
              )
            ) : (
              current.recipient && (
                <span className="approval-recipient">→ {current.recipient}</span>
              )
            )}
          </div>
          {current.subject && <div className="approval-subject">{current.subject}</div>}
          <textarea
            className={`approval-body-edit${isBash ? " approval-command-edit" : ""}`}
            value={body}
            disabled={busy}
            onChange={(e) => setBody(e.target.value)}
            rows={isBash ? 4 : 8}
          />
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
                  if (e.key === "Enter") runReject();
                }}
              />
              <button
                type="button"
                className="confirm-delete"
                disabled={busy}
                onClick={runReject}
              >
                {busy ? "Rejecting…" : "Confirm"}
              </button>
              <button
                type="button"
                className="confirm-cancel"
                disabled={busy}
                onClick={() => {
                  setRejecting(false);
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
                onClick={runApprove}
              >
                {busy ? "Approving…" : "Approve"}
              </button>
              {canAlwaysAllow && (
                <button
                  type="button"
                  className="head-action approval-always"
                  disabled={busy}
                  title={`Adds "${allowItem}" to the allowlist, then approves`}
                  onClick={runAlwaysAllow}
                >
                  {busy ? "Allowing…" : `Approve & always allow “${allowLabel}”`}
                </button>
              )}
              <button
                type="button"
                className="confirm-delete approval-reject"
                disabled={busy}
                onClick={() => {
                  setRejecting(true);
                  setReason("");
                }}
              >
                Reject
              </button>
            </div>
          )}
        </div>
        <div className="confirm-actions">
          <button
            type="button"
            className="confirm-cancel"
            disabled={busy}
            onClick={onClose}
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
