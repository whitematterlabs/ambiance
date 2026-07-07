// Phrase activation — the **cloud/remote fallback** path for hands-free voice.
//
// The primary phrase-activation path is the local `voice` driver: the host mic
// runs openWakeWord + whisper on-device and the kernel routes `voice:utterance`
// straight to the PAI (the web only renders a "Speaking: …" indicator). This
// file is what runs when there is no local host listener — e.g. the remote
// (ngrok) surface, or a machine without the `voice` driver installed. It drives
// a continuous browser SpeechRecognition session that listens for the wake word
// and submits the words after it, using the browser's own transcription so it
// works even when no server STT key is present.
//
// SpeechRecognition is still vendor-prefixed (webkit) and untyped in lib.dom,
// so everything here is `any` behind a narrow surface.

import { useEffect, useRef } from "react";

// Matches the local driver's openWakeWord model (`alexa`), so the same word
// works whether the host mic or this browser fallback is doing the listening.
export const DEFAULT_WAKE_PHRASE = "alexa";

type SpeechRecognitionCtor = new () => any;

function recognitionCtor(): SpeechRecognitionCtor | null {
  if (typeof window === "undefined") return null;
  const w = window as any;
  return (w.SpeechRecognition || w.webkitSpeechRecognition || null) as SpeechRecognitionCtor | null;
}

export function speechRecognitionSupported(): boolean {
  return recognitionCtor() !== null;
}

export interface PhraseActivationOptions {
  enabled: boolean;
  phrase: string;
  onCommand: (text: string) => void;
  onStatus?: (message: string) => void;
  // Returns true while we should ignore the mic (e.g. PAI is speaking) so the
  // wake word can't be triggered by our own TTS bleeding into the mic.
  isMuted?: () => boolean;
  // Returns true while a follow-up window is open (the PAI just finished
  // talking): the next final transcript is submitted as a command verbatim,
  // no wake phrase needed. The wake phrase still works during the window.
  inFollowUp?: () => boolean;
}

// Drives a continuous SpeechRecognition session for the lifetime of `enabled`.
// Restarts itself when the browser ends the session on silence.
export function usePhraseActivation(options: PhraseActivationOptions): void {
  const { enabled, phrase } = options;
  // Keep callbacks fresh without re-subscribing the recogniser every render.
  const optsRef = useRef(options);
  optsRef.current = options;

  useEffect(() => {
    if (!enabled) return;
    const Ctor = recognitionCtor();
    if (!Ctor) {
      optsRef.current.onStatus?.("voice: phrase activation isn't supported in this browser");
      return;
    }

    const needle = phrase.trim().toLowerCase();
    if (!needle) return;

    let disposed = false;

    // Pre-warm the mic permission the moment phrase activation turns on, rather
    // than waiting for a PTT press to be the thing that ever prompts. Browser
    // SpeechRecognition silently no-ops (or dies with `not-allowed`) when the
    // microphone permission hasn't been granted yet, which is why the wake word
    // looked dead until you'd hit push-to-talk at least once. Grabbing — and
    // immediately releasing — a getUserMedia stream forces the permission dialog
    // up front; the recogniser below then has the grant it needs to actually
    // hear anything.
    void (async () => {
      try {
        const stream = await navigator.mediaDevices?.getUserMedia?.({ audio: true });
        // We only wanted the permission grant — don't hold the mic open.
        stream?.getTracks().forEach((track) => track.stop());
      } catch {
        /* denied or unavailable — rec.onerror surfaces the not-allowed below */
      }
    })();

    const rec = new Ctor();
    rec.continuous = true;
    rec.interimResults = false;
    rec.lang = navigator.language || "en-US";

    rec.onresult = (event: any) => {
      if (optsRef.current.isMuted?.()) return;
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        if (!result?.isFinal) continue;
        const said = String(result[0]?.transcript ?? "").trim();
        if (!said) continue;
        const idx = said.toLowerCase().indexOf(needle);
        if (idx === -1) {
          // No wake phrase — but if the PAI just finished talking, treat the
          // whole utterance as a follow-up command.
          if (optsRef.current.inFollowUp?.()) {
            optsRef.current.onStatus?.("voice: follow-up heard → sending");
            optsRef.current.onCommand(said);
          }
          continue;
        }
        const command = said
          .slice(idx + needle.length)
          .replace(/^[\s,.:;!?\-—]+/, "")
          .trim();
        if (command) {
          optsRef.current.onStatus?.(`voice: heard "${phrase}" → sending`);
          optsRef.current.onCommand(command);
        } else {
          optsRef.current.onStatus?.(`voice: heard "${phrase}" — say a command`);
        }
      }
    };

    rec.onerror = (event: any) => {
      const err = event?.error;
      // no-speech/aborted fire constantly during normal silence; ignore them.
      if (err && err !== "no-speech" && err !== "aborted" && err !== "audio-capture") {
        optsRef.current.onStatus?.(`voice: phrase activation error (${err})`);
      }
      if (err === "not-allowed" || err === "service-not-allowed") {
        disposed = true; // permission denied — don't fight the browser
      }
    };

    rec.onend = () => {
      // Browsers cut continuous recognition after a silence window. Restart
      // while the user still wants it on.
      if (disposed) return;
      try {
        rec.start();
      } catch {
        /* already (re)starting — ignore */
      }
    };

    try {
      rec.start();
      optsRef.current.onStatus?.(`voice: listening for "${phrase}"`);
    } catch {
      /* start() throws if a prior session is still tearing down — onend retries */
    }

    return () => {
      disposed = true;
      rec.onresult = null;
      rec.onend = null;
      rec.onerror = null;
      try {
        rec.stop();
      } catch {
        /* noop */
      }
    };
  }, [enabled, phrase]);
}
