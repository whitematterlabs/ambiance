// Voice *input* on the web surface. Two activation styles share this file's
// helpers; the components own the UI:
//
//   • Push-to-talk  — hold the composer mic, release to transcribe + send.
//     Lives in MessageInput (MediaRecorder → /api/stt), this file only exposes
//     the wake-word path.
//   • Phrase activation — hands-free. A continuous browser SpeechRecognition
//     session listens for a wake phrase ("hey alexa"); the words after it are
//     submitted as a message. This uses the browser's own transcription, so it
//     works even when the server STT key is absent.
//
// SpeechRecognition is still vendor-prefixed (webkit) and untyped in lib.dom,
// so everything here is `any` behind a narrow surface.

import { useEffect, useRef } from "react";

export const DEFAULT_WAKE_PHRASE = "hey alexa";

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
        if (idx === -1) continue;
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
