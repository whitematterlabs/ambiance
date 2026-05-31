import { useState } from "react";

// The "password" path of remote access: when an /api/* call 401s (someone
// opened the tunnel URL without the QR's `?token=`), this one-field overlay
// takes the access code, persists it, and reloads. The QR is the "scan" path;
// both resolve to the same token. Not dismissable — without a code there's
// nothing to show.
export function LoginGate({ onSubmit }: { onSubmit: (code: string) => void }) {
  const [code, setCode] = useState("");
  const submit = () => {
    const trimmed = code.trim();
    if (trimmed) onSubmit(trimmed);
  };

  return (
    <div className="login-overlay">
      <div className="login-card">
        <h2 className="login-title">PAI remote access</h2>
        <p className="login-copy">
          Enter the access code shown in the PAI app to connect.
        </p>
        <input
          className="login-input"
          placeholder="access code"
          autoFocus
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          value={code}
          onChange={(e) => setCode(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
          }}
        />
        <button className="login-button" type="button" onClick={submit} disabled={!code.trim()}>
          Connect
        </button>
      </div>
    </div>
  );
}
