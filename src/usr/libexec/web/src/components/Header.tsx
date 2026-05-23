import { useEffect, useState } from "react";

export function Header({
  connected,
  provider,
  busyCount,
  onOpenPalette,
}: {
  connected: boolean;
  provider: string;
  busyCount: number;
  onOpenPalette: () => void;
}) {
  const [clock, setClock] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setClock(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <header className="header">
      <div className="brand">
        <div className="brand-mark">P</div>
        <div className="brand-copy">
          <span className="title">PAI</span>
          <span className="subtitle">Web workspace</span>
        </div>
      </div>
      <span className="spacer" />
      <span className="busy-chip">{busyCount ? `${busyCount} active` : "All idle"}</span>
      <button className="provider-button" type="button" onClick={onOpenPalette}>
        Provider: {provider}
      </button>
      <span className={`conn ${connected ? "on" : "off"}`}>
        <span className="conn-dot" />
        {connected ? "Connected" : "Disconnected"}
      </span>
      <span className="clock">{clock.toLocaleTimeString()}</span>
    </header>
  );
}
