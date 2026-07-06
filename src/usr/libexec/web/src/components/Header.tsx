import { useEffect, useRef, useState } from "react";
import { HelpCircle, Loader, Moon, Pause, Play, Smartphone, Sun } from "lucide-react";
import { Logo } from "./Logo";
import { VoiceSettings } from "./VoiceSettings";
import type { VoiceEngine } from "../speech";

export function Header({
  connected,
  kernelRunning,
  kernelBusy,
  onToggleKernel,
  sidebarOpen,
  onToggleSidebar,
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
  hostManaged,
  wakePhrase,
  onShowWelcome,
  onSetupRemote,
}: {
  connected: boolean;
  kernelRunning: boolean;
  kernelBusy: boolean;
  onToggleKernel: () => void;
  sidebarOpen: boolean;
  onToggleSidebar: () => void;
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
  hostManaged?: boolean;
  // True when the local `voice` driver's host-mic listener is running. Then
  // phrase activation rides the host mic (kernel-side wake + STT) regardless of
  // browser SpeechRecognition support; otherwise it falls back to the browser.
  wakePhrase: string;
  onShowWelcome: () => void;
  onSetupRemote: () => void;
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

  return (
    <header className="header">
      <div className="brand">
        <button
          className="brand-logo-btn"
          type="button"
          onClick={onToggleSidebar}
          aria-pressed={sidebarOpen}
          title={sidebarOpen ? "Hide sidebar" : "Show sidebar"}
          aria-label={sidebarOpen ? "Hide sidebar" : "Show sidebar"}
        >
          <Logo className="brand-logo" />
        </button>
        <button
          className={`kernel-toggle${kernelRunning ? " running" : ""}`}
          type="button"
          disabled={!connected || kernelBusy}
          onClick={onToggleKernel}
          aria-pressed={kernelRunning}
          title={
            kernelBusy
              ? "Working…"
              : kernelRunning
                ? "Stop kernel"
                : "Start kernel"
          }
          aria-label={kernelRunning ? "Stop kernel" : "Start kernel"}
        >
          {kernelBusy ? (
            <Loader className="spin" size={15} aria-hidden="true" />
          ) : kernelRunning ? (
            <Pause size={15} aria-hidden="true" />
          ) : (
            <Play size={15} aria-hidden="true" />
          )}
        </button>
      </div>
      <span className="spacer" />
      <button
        className="ghost-button theme-toggle"
        type="button"
        onClick={onSetupRemote}
        title="Set up mobile access — ask root to tunnel this console via ngrok"
        aria-label="Set up mobile access"
      >
        <Smartphone size={15} aria-hidden="true" />
      </button>
      <button
        className="ghost-button theme-toggle"
        type="button"
        onClick={onShowWelcome}
        title="What can PAI do?"
        aria-label="What can PAI do?"
      >
        <HelpCircle size={15} aria-hidden="true" />
      </button>
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
                hostManaged={hostManaged}
              wakePhrase={wakePhrase}
            />
          </div>
        )}
      </div>
    </header>
  );
}
