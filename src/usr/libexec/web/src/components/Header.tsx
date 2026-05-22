import { useEffect, useState } from "react";

export function Header({ connected }: { connected: boolean }) {
  const [clock, setClock] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setClock(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <header className="header">
      <span className="title">PAI — operator console</span>
      <span className="spacer" />
      <span className={`conn ${connected ? "on" : "off"}`}>
        {connected ? "● attached" : "○ detached"}
      </span>
      <span className="clock">{clock.toLocaleTimeString()}</span>
    </header>
  );
}
