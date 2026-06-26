import { useEffect, useRef, useState } from "react";
import { Moon, Sun } from "lucide-react";
import { VOICE_OPTIONS } from "../speech";

export function Header({
  connected,
  kernelRunning,
  kernelBusy,
  onToggleKernel,
  theme,
  onToggleTheme,
  voiceEnabled,
  onToggleVoice,
  voiceId,
  voiceSpeed,
  onVoiceIdChange,
  onVoiceSpeedChange,
}: {
  connected: boolean;
  kernelRunning: boolean;
  kernelBusy: boolean;
  onToggleKernel: () => void;
  theme: "light" | "dark";
  onToggleTheme: () => void;
  voiceEnabled: boolean;
  onToggleVoice: () => void;
  voiceId: string | null;
  voiceSpeed: number;
  onVoiceIdChange: (id: string | null) => void;
  onVoiceSpeedChange: (speed: number) => void;
}) {
  const [pickerOpen, setPickerOpen] = useState(false);
  const popoverRef = useRef<HTMLDivElement | null>(null);

  // Close on outside click / Escape so the popover behaves like a menu.
  useEffect(() => {
    if (!pickerOpen) return;
    const onDocMouseDown = (e: MouseEvent) => {
      if (popoverRef.current && !popoverRef.current.contains(e.target as Node)) {
        setPickerOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPickerOpen(false);
    };
    document.addEventListener("mousedown", onDocMouseDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocMouseDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [pickerOpen]);

  const selectedName =
    voiceId === null
      ? "Server default"
      : VOICE_OPTIONS.find((v) => v.id === voiceId)?.name ?? "Custom";

  return (
    <header className="header">
      <div className="brand">
        <span className="brand-name">PAI</span>
        <span
          className={`conn-status ${connected ? "on" : "off"}`}
          role="status"
          aria-label={connected ? "Connected" : "Disconnected"}
          title={connected ? "Connected" : "Disconnected"}
        >
          {connected ? "Online" : "Offline"}
        </span>
        <button
          className="kernel-toggle"
          type="button"
          disabled={!connected || kernelBusy}
          onClick={onToggleKernel}
          title={kernelRunning ? "Stop kernel" : "Start kernel"}
        >
          {kernelBusy ? "Kernel..." : kernelRunning ? "Stop kernel" : "Start kernel"}
        </button>
      </div>
      <span className="spacer" />
      <button
        className="ghost-button theme-toggle"
        type="button"
        onClick={onToggleTheme}
        aria-pressed={theme === "dark"}
        title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
        aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
      >
        {theme === "dark" ? (
          <Sun size={15} aria-hidden="true" />
        ) : (
          <Moon size={15} aria-hidden="true" />
        )}
      </button>
      <div className="voice-split" ref={popoverRef}>
        <button
          className="ghost-button voice-toggle"
          type="button"
          onClick={onToggleVoice}
          aria-pressed={voiceEnabled}
          title={voiceEnabled ? "Voice on — reading replies aloud" : "Voice off"}
        >
          <span className="ghost-label">{voiceEnabled ? "Voice on" : "Voice off"}</span>
        </button>
        <button
          className="ghost-button voice-chevron"
          type="button"
          onClick={() => setPickerOpen((v) => !v)}
          aria-haspopup="dialog"
          aria-expanded={pickerOpen}
          title="Voice settings"
        >
          <svg
            width="10"
            height="10"
            viewBox="0 0 10 10"
            aria-hidden="true"
            focusable="false"
          >
            <path
              d="M1.5 3.5L5 7L8.5 3.5"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.4"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </button>
        {pickerOpen && (
          <div className="voice-popover" role="dialog" aria-label="Voice settings">
            <div className="voice-popover-head">
              <span className="voice-popover-title">Voice</span>
              <span className="voice-popover-current">{selectedName}</span>
            </div>
            <ul className="voice-list">
              <li>
                <button
                  type="button"
                  className={`voice-item ${voiceId === null ? "selected" : ""}`}
                  onClick={() => onVoiceIdChange(null)}
                >
                  <span className="voice-name">Server default</span>
                  <span className="voice-blurb">Whatever .env / Rachel</span>
                </button>
              </li>
              {VOICE_OPTIONS.map((v) => (
                <li key={v.id}>
                  <button
                    type="button"
                    className={`voice-item ${voiceId === v.id ? "selected" : ""}`}
                    onClick={() => onVoiceIdChange(v.id)}
                  >
                    <span className="voice-name">{v.name}</span>
                    <span className="voice-blurb">{v.blurb}</span>
                  </button>
                </li>
              ))}
            </ul>
            <div className="voice-speed">
              <label htmlFor="voice-speed-input" className="voice-speed-label">
                Speed
                <span className="voice-speed-value">{voiceSpeed.toFixed(2)}×</span>
              </label>
              <input
                id="voice-speed-input"
                type="range"
                min={0.7}
                max={1.2}
                step={0.05}
                value={voiceSpeed}
                onChange={(e) => onVoiceSpeedChange(parseFloat(e.target.value))}
              />
            </div>
          </div>
        )}
      </div>
    </header>
  );
}
