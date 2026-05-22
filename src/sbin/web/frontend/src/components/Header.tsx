import { useEffect, useState } from "react";

export function Header({
  provider,
  connected,
  onPalette,
}: {
  provider: string;
  connected: boolean;
  onPalette: () => void;
}) {
  const [clock, setClock] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setClock(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <header className="header">
      <span className="title">PAI — operator console</span>
      <span className="spacer" />
      <button className="provider-chip" onClick={onPalette} title="⌘K — commands">
        provider: {provider}
      </button>
      <span className={`conn ${connected ? "on" : "off"}`}>
        {connected ? "● attached" : "○ detached"}
      </span>
      <span className="clock">{clock.toLocaleTimeString()}</span>
    </header>
  );
}
