import { useEffect, useRef, useState } from "react";
import { HelpCircle, Loader, Mic, Moon, Pause, Play, Smartphone, Sun } from "lucide-react";
import { Logo } from "./Logo";
import { VoiceSettings } from "./VoiceSettings";
import type { VoiceEngine } from "../speech";
import type { SendCapability, SendMode } from "../types";
import { CAPTURE_COPY, COWORK_FLAGS, COWORK_PILL } from "../capture";

// Popover open-state that closes on outside click / Escape, so each split
// button (voice, cowork) behaves like a menu without duplicating listeners.
function useDismissablePopover() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const onDocMouseDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDocMouseDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocMouseDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return { open, setOpen, ref };
}

// The chevron glyph both split buttons share.
function Chevron() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true" focusable="false">
      <path
        d="M1.5 3.5L5 7L8.5 3.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

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
  onEnableVoice,
  pushToTalk,
  onTogglePushToTalk,
  phraseActivation,
  onTogglePhraseActivation,
  phraseSupported,
  hostManaged,
  wakePhrase,
  captureCaps,
  onSetCaptureMode,
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
  onEnableVoice: () => void;
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
  // Mounted capture gates (cowork facets/notetaker) — one split button next
  // to voice: the pill toggles the cowork facets as a group, the chevron opens
  // per-facet switches for every gate. Renders nothing when no gate is mounted.
  captureCaps: SendCapability[];
  onSetCaptureMode: (flag: string, mode: SendMode) => void;
  onShowWelcome: () => void;
  onSetupRemote: () => void;
}) {
  const voicePicker = useDismissablePopover();
  const capturePicker = useDismissablePopover();

  // The split button toggles the cowork facets as a group (on = any facet
  // on; a click drives them all to the same state); the popover holds the
  // per-facet switches plus every other capture gate (Notes included). The
  // pill is always labeled "Coworking"; when no cowork facet is mounted it
  // drives the first gate so Notes-only fleets still get a toggle.
  const coworkCaps = captureCaps.filter((c) => COWORK_FLAGS.has(c.flag));
  const pillCaps = coworkCaps.length > 0 ? coworkCaps : captureCaps.slice(0, 1);
  const pillOn = pillCaps.some((c) => c.mode === "yes");
  const setPillMode = (mode: SendMode) => {
    for (const cap of pillCaps) {
      if (cap.mode !== mode) onSetCaptureMode(cap.flag, mode);
    }
  };

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
        title="What can Ambiance do?"
        aria-label="What can Ambiance do?"
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
      {pillCaps.length > 0 && (
        <div className="voice-split capture-split" ref={capturePicker.ref}>
          <button
            className="ghost-button voice-toggle capture-toggle"
            type="button"
            onClick={() => setPillMode(pillOn ? "no" : "yes")}
            aria-pressed={pillOn}
            title={pillOn ? COWORK_PILL.onHint : COWORK_PILL.offHint}
          >
            <span className="ghost-label">
              {`${COWORK_PILL.name} ${pillOn ? "on" : "off"}`}
            </span>
          </button>
          <button
            className="ghost-button voice-chevron"
            type="button"
            onClick={() => capturePicker.setOpen((v) => !v)}
            aria-haspopup="dialog"
            aria-expanded={capturePicker.open}
            title="Coworking settings"
          >
            <Chevron />
          </button>
          {capturePicker.open && (
            <div className="voice-popover" role="dialog" aria-label="Coworking settings">
              <div className="voice-section">
                <span className="voice-section-title">Coworking</span>
                {captureCaps.map((cap) => {
                  const copy = CAPTURE_COPY[cap.flag];
                  const on = cap.mode === "yes";
                  return (
                    <button
                      key={cap.flag}
                      type="button"
                      className="voice-switch"
                      role="switch"
                      aria-checked={on}
                      onClick={() => onSetCaptureMode(cap.flag, on ? "no" : "yes")}
                    >
                      <span className="voice-switch-copy">
                        <span className="voice-switch-name">{cap.channel}</span>
                        <span className="voice-switch-blurb">{copy?.blurb ?? ""}</span>
                      </span>
                      <span className="voice-switch-track" aria-hidden="true">
                        <span className="voice-switch-thumb" />
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}
      {phraseActivation && (
        // A hot mic is never implicit: whenever phrase activation is
        // listening (host driver or browser fallback), this indicator is
        // visible regardless of the read-aloud "Voice" toggle next to it.
        // One click turns the listener off.
        <button
          className="ghost-button mic-indicator"
          type="button"
          onClick={onTogglePhraseActivation}
          title={`${hostManaged ? "The host mic" : "This browser's mic"} is listening for "${wakePhrase}" — click to turn it off`}
          aria-label="Microphone is listening — turn off"
        >
          <Mic size={15} aria-hidden="true" />
          <span className="ghost-label">Mic on</span>
        </button>
      )}
      <div className="voice-split" ref={voicePicker.ref}>
        <button
          className="ghost-button voice-toggle"
          type="button"
          onClick={onToggleVoice}
          aria-pressed={voiceEnabled}
          title={
            voiceEnabled
              ? "Voice on — reading replies aloud"
              : "Voice off — replies aren't read aloud (mic listening is separate — see voice settings)"
          }
        >
          <span className="ghost-label">{voiceEnabled ? "Voice on" : "Voice off"}</span>
        </button>
        <button
          className="ghost-button voice-chevron"
          type="button"
          onClick={() => voicePicker.setOpen((v) => !v)}
          aria-haspopup="dialog"
          aria-expanded={voicePicker.open}
          title="Voice settings"
        >
          <Chevron />
        </button>
        {voicePicker.open && (
          <div className="voice-popover" role="dialog" aria-label="Voice settings">
            <VoiceSettings
              voiceId={voiceId}
              voiceSpeed={voiceSpeed}
              onVoiceIdChange={onVoiceIdChange}
              onVoiceSpeedChange={onVoiceSpeedChange}
              voiceEngine={voiceEngine}
              onVoiceEngineChange={onVoiceEngineChange}
              onEnableVoice={onEnableVoice}
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
