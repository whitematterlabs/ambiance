import { VOICE_OPTIONS, type VoiceEngine } from "../speech";

// The body of the voice configuration panel — activation switches, the
// read-aloud voice list, and the speed slider. Shared by the desktop Header
// popover and the mobile menu sheet so the two never drift.
export function VoiceSettings({
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
  hostManaged = false,
  wakePhrase,
  showHead = true,
}: {
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
  // When true, the host has the local `voice` driver installed and this switch
  // starts/stops it (the host mic) — so it's operable even while stopped.
  hostManaged?: boolean;
  wakePhrase: string;
  // The "Voice / <current>" header is useful in the floating popover but
  // redundant in the mobile sheet, which already has its own section heading.
  showHead?: boolean;
}) {
  const selectedName =
    voiceEngine === "siri"
      ? "Siri"
      : voiceId === null
        ? "Server default"
        : VOICE_OPTIONS.find((v) => v.id === voiceId)?.name ?? "Custom";

  return (
    <>
      {showHead && (
        <div className="voice-popover-head">
          <span className="voice-popover-title">Voice</span>
          <span className="voice-popover-current">{selectedName}</span>
        </div>
      )}
      <div className="voice-section">
        <span className="voice-section-title">Activation</span>
        <button
          type="button"
          className="voice-switch"
          role="switch"
          aria-checked={pushToTalk}
          onClick={onTogglePushToTalk}
        >
          <span className="voice-switch-copy">
            <span className="voice-switch-name">Push-to-talk</span>
            <span className="voice-switch-blurb">Composer mic: hold to record, release to send</span>
          </span>
          <span className="voice-switch-track" aria-hidden="true">
            <span className="voice-switch-thumb" />
          </span>
        </button>
        <button
          type="button"
          className="voice-switch"
          role="switch"
          // With the local `voice` driver installed, this switch is the real
          // on/off for the host mic — start/stops the voice-in driver, so it
          // stays operable even while the listener is off. Without the driver,
          // it toggles the browser fallback (needs Web Speech API support).
          aria-checked={phraseActivation}
          onClick={onTogglePhraseActivation}
          disabled={!hostManaged && !phraseSupported}
          title={
            hostManaged
              ? phraseActivation
                ? "The host mic is listening for the wake word — toggle off to stop it."
                : "The host mic is off — toggle on to start listening for the wake word."
              : phraseSupported
                ? undefined
                : "Phrase activation needs the local voice driver or the Web Speech API (try Chrome or Edge)"
          }
        >
          <span className="voice-switch-copy">
            <span className="voice-switch-name">
              Phrase activation
              {hostManaged && <span className="voice-switch-tag">host mic</span>}
            </span>
            <span className="voice-switch-blurb">
              {hostManaged ? (
                phraseActivation ? (
                  <>
                    On — the local voice driver is listening for <em>"{wakePhrase}"</em>
                  </>
                ) : (
                  <>Off — the host mic isn't listening</>
                )
              ) : phraseSupported ? (
                <>
                  Say <em>"{wakePhrase}"</em> to talk
                </>
              ) : (
                "Not supported in this browser"
              )}
            </span>
          </span>
          <span className="voice-switch-track" aria-hidden="true">
            <span className="voice-switch-thumb" />
          </span>
        </button>
      </div>
      <span className="voice-section-title voice-section-title--list">Read aloud</span>
      <div className="voice-engine" role="radiogroup" aria-label="Read-aloud engine">
        <button
          type="button"
          role="radio"
          aria-checked={voiceEngine === "elevenlabs"}
          className={`voice-engine-option ${voiceEngine === "elevenlabs" ? "selected" : ""}`}
          onClick={() => onVoiceEngineChange("elevenlabs")}
        >
          <span className="voice-engine-name">ElevenLabs</span>
          <span className="voice-engine-blurb">Cloud voices</span>
        </button>
        <button
          type="button"
          role="radio"
          aria-checked={voiceEngine === "siri"}
          className={`voice-engine-option ${voiceEngine === "siri" ? "selected" : ""}`}
          onClick={() => onVoiceEngineChange("siri")}
        >
          <span className="voice-engine-name">Siri</span>
          <span className="voice-engine-blurb">macOS, on-device</span>
        </button>
      </div>
      {voiceEngine === "elevenlabs" ? (
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
      ) : (
        <p className="voice-engine-note">
          Siri reads with your macOS system voice — change it in System Settings
          → Accessibility → Spoken Content.
        </p>
      )}
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
    </>
  );
}
