import { useEffect, useState } from "react";
import { HelpCircle, Menu, Moon, Smartphone, Sun, X } from "lucide-react";
import type { FleetMember, ProcRow } from "../types";
import { paiColor } from "../palette";
import { Logo } from "./Logo";
import { VoiceSettings } from "./VoiceSettings";
import type { VoiceEngine } from "../speech";

// Mobile-only top bar. On the go the desktop chrome (kernel/voice/theme
// controls, fleet strip, activity tab, status line) is too much for a phone, so
// it all collapses behind a single menu button and the screen is just the chat.
export function MobileMenu({
  connected,
  fleet,
  procs,
  activePid,
  onSelect,
  activeLabel,
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
  voiceEngine,
  onVoiceEngineChange,
  pushToTalk,
  onTogglePushToTalk,
  phraseActivation,
  onTogglePhraseActivation,
  phraseSupported,
  localListener,
  wakePhrase,
  onShowWelcome,
  onSetupRemote,
  onClear,
  clearBusy,
  onCompact,
}: {
  connected: boolean;
  fleet: FleetMember[];
  procs: ProcRow[];
  activePid: number | null;
  onSelect: (pid: number) => void;
  activeLabel: string;
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
  voiceEngine: VoiceEngine;
  onVoiceEngineChange: (engine: VoiceEngine) => void;
  pushToTalk: boolean;
  onTogglePushToTalk: () => void;
  phraseActivation: boolean;
  onTogglePhraseActivation: () => void;
  phraseSupported: boolean;
  localListener: boolean;
  wakePhrase: string;
  onShowWelcome: () => void;
  onSetupRemote: () => void;
  onClear: () => void;
  clearBusy: boolean;
  onCompact: () => void;
}) {
  const [open, setOpen] = useState(false);

  // Close on Escape, and lock body scroll while the sheet is up.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  const close = () => setOpen(false);
  const busyPids = new Set(procs.filter((r) => r.busy).map((r) => r.pid));

  return (
    <div className="mobile-bar">
      <div className="mobile-bar-brand">
        <Logo className="brand-logo" />
        <span className="mobile-bar-title">{activeLabel}</span>
      </div>
      <button
        className="mobile-bar-menu"
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={open ? "Close menu" : "Open menu"}
      >
        {open ? <X size={20} aria-hidden="true" /> : <Menu size={20} aria-hidden="true" />}
      </button>

      {open && (
        <>
          <div className="mobile-sheet-backdrop" onClick={close} aria-hidden="true" />
          <div className="mobile-sheet" role="dialog" aria-label="Menu">
            <section className="mobile-sheet-group">
              <h2 className="mobile-sheet-heading">PAIs</h2>
              <div className="mobile-pai-list">
                {fleet.length === 0 && <p className="mobile-sheet-empty">No running PAIs</p>}
                {fleet.map((m) => {
                  const busy = busyPids.has(String(m.pid));
                  const label = m.title || m.slug;
                  return (
                    <button
                      key={m.pid}
                      type="button"
                      className={`mobile-pai ${m.pid === activePid ? "active" : ""}`}
                      style={{ ["--pai-color" as string]: paiColor(m.slug || m.pid) }}
                      onClick={() => {
                        onSelect(m.pid);
                        close();
                      }}
                    >
                      <span className="mobile-pai-name">{label}</span>
                      <span className="mobile-pai-meta">
                        {busy ? "Working" : "Ready"}
                        {m.fallback ? " / Default" : ""}
                      </span>
                    </button>
                  );
                })}
              </div>
            </section>

            <section className="mobile-sheet-group">
              <h2 className="mobile-sheet-heading">Conversation</h2>
              <div className="mobile-sheet-actions">
                <button
                  type="button"
                  className="mobile-sheet-action"
                  disabled={activePid === null || clearBusy}
                  onClick={() => {
                    onClear();
                    close();
                  }}
                >
                  {clearBusy ? "Clearing…" : "Clear"}
                </button>
                <button
                  type="button"
                  className="mobile-sheet-action"
                  disabled={activePid === null}
                  onClick={() => {
                    onCompact();
                    close();
                  }}
                >
                  Compact
                </button>
              </div>
            </section>

            <section className="mobile-sheet-group">
              <h2 className="mobile-sheet-heading">Voice</h2>
              <button
                type="button"
                className="voice-switch"
                role="switch"
                aria-checked={voiceEnabled}
                onClick={onToggleVoice}
              >
                <span className="voice-switch-copy">
                  <span className="voice-switch-name">Read replies aloud</span>
                  <span className="voice-switch-blurb">
                    {voiceEnabled ? "Voice on" : "Voice off"}
                  </span>
                </span>
                <span className="voice-switch-track" aria-hidden="true">
                  <span className="voice-switch-thumb" />
                </span>
              </button>
              <VoiceSettings
                voiceId={voiceId}
                voiceSpeed={voiceSpeed}
                onVoiceIdChange={onVoiceIdChange}
                onVoiceSpeedChange={onVoiceSpeedChange}
                voiceEngine={voiceEngine}
                onVoiceEngineChange={onVoiceEngineChange}
                pushToTalk={pushToTalk}
                onTogglePushToTalk={onTogglePushToTalk}
                phraseActivation={phraseActivation}
                onTogglePhraseActivation={onTogglePhraseActivation}
                phraseSupported={phraseSupported}
                localListener={localListener}
                wakePhrase={wakePhrase}
                showHead={false}
              />
            </section>

            <section className="mobile-sheet-group">
              <h2 className="mobile-sheet-heading">System</h2>
              <div className="mobile-sheet-rows">
                <button
                  type="button"
                  className="mobile-sheet-row"
                  disabled={!connected || kernelBusy}
                  onClick={() => {
                    onToggleKernel();
                    close();
                  }}
                >
                  {kernelBusy ? "Kernel…" : kernelRunning ? "Stop kernel" : "Start kernel"}
                </button>
                <button
                  type="button"
                  className="mobile-sheet-row"
                  onClick={() => {
                    onToggleTheme();
                  }}
                >
                  {theme === "dark" ? <Sun size={15} /> : <Moon size={15} />}
                  {theme === "dark" ? "Light mode" : "Dark mode"}
                </button>
                <button
                  type="button"
                  className="mobile-sheet-row"
                  onClick={() => {
                    onSetupRemote();
                    close();
                  }}
                >
                  <Smartphone size={15} />
                  Set up mobile access
                </button>
                <button
                  type="button"
                  className="mobile-sheet-row"
                  onClick={() => {
                    onShowWelcome();
                    close();
                  }}
                >
                  <HelpCircle size={15} />
                  What can PAI do?
                </button>
              </div>
            </section>
          </div>
        </>
      )}
    </div>
  );
}
