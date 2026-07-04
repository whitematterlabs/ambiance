// Voice mode's frontend swap point: a speech backend behind a small interface,
// plus a sequential queue so utterances never overlap. The server chooses the
// actual TTS engine; today that is ElevenLabs when configured, otherwise macOS
// `say`. Swapping the browser-side transport later still means writing another
// SpeechBackend and constructing the queue with it.

// Curated subset of the ElevenLabs public voice library. The dialog in Header
// shows these; users with ELEVENLABS_VOICE_ID set in .env can still hit "Server
// default" to ignore the per-session pick. If the server falls back to macOS
// `say`, these per-session voice IDs are ignored and the system default voice is
// used.
export interface VoiceOption {
  id: string;
  name: string;
  blurb: string;
}

// The read-aloud engine. "elevenlabs" is the premium default; "siri" is macOS
// `say`, used automatically when no ElevenLabs key is configured.
export type VoiceEngine = "elevenlabs" | "siri";

export const VOICE_OPTIONS: VoiceOption[] = [
  { id: "21m00Tcm4TlvDq8ikWAM", name: "Rachel", blurb: "Calm, warm narrator" },
  { id: "EXAVITQu4vr4xnSDxMaL", name: "Bella", blurb: "Soft, friendly" },
  { id: "AZnzlk1HvdvWOWPv4f5WU", name: "Domi", blurb: "Confident, upbeat" },
  { id: "MF3mGyEYCl7XYWbV9V6O", name: "Elli", blurb: "Young, expressive" },
  { id: "ErXwobaYiN019PkySvjV", name: "Antoni", blurb: "Well-rounded male" },
  { id: "pNInz6obpgDQGcFmaJgB", name: "Adam", blurb: "Deep, grounded" },
  { id: "TxGEqnHWrfWFTfGW9XjX", name: "Josh", blurb: "Casual, conversational" },
  { id: "VR6AewLTigWG4xSOukaG", name: "Arnold", blurb: "Crisp, authoritative" },
];

import { authHeaders } from "./auth";

export interface SpeechBackend {
  speak(text: string): Promise<void>; // resolves when audio finishes
  cancel(): void; // stop current + drop in-flight
}

// Optional callback so the queue can report failures to the UI (status bar)
// instead of failures being console-only. Kept as a property to avoid widening
// the backend constructor — the queue owns this.
export type SpeechErrorReporter = (message: string) => void;

// v1 backend: POST text to the local /api/tts proxy, play the returned audio
// blob through a single reused <audio> element.
export class ServerSpeechBackend implements SpeechBackend {
  private audio: HTMLAudioElement;
  private currentUrl: string | null = null;
  onError: SpeechErrorReporter | null = null;
  // Per-session voice + speed; the dialog in Header mutates these directly.
  // `null` voiceId means "let the server pick" (env / built-in default).
  voiceId: string | null = null;
  speed: number = 1.1;
  // Read-aloud engine: "elevenlabs" (cloud) or "siri" (macOS `say`). The server
  // maps this to a provider package and, when ElevenLabs has no key, falls back
  // to Siri on its own — so this is a preference, not a hard requirement.
  engine: VoiceEngine = "elevenlabs";

  constructor() {
    this.audio = new Audio();
  }

  private report(msg: string): void {
    console.warn(msg);
    this.onError?.(msg);
  }

  async speak(text: string): Promise<void> {
    let url: string | null = null;
    try {
      const res = await fetch("/api/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({
          text,
          ...(this.voiceId ? { voice_id: this.voiceId } : {}),
          speed: this.speed,
          engine: this.engine,
        }),
      });
      if (!res.ok) {
        let detail = `${res.status}`;
        try {
          const j = await res.json();
          if (j && typeof j.error === "string") detail = `${res.status} — ${j.error}`;
        } catch {
          // body wasn't JSON; surface the status alone
        }
        this.report(`voice: tts server returned ${detail}`);
        return; // fail quietly so one failure doesn't wedge the queue
      }
      const blob = await res.blob();
      url = URL.createObjectURL(blob);
      this.currentUrl = url;
      this.audio.src = url;
      await new Promise<void>((resolve) => {
        const done = () => {
          this.audio.removeEventListener("ended", done);
          this.audio.removeEventListener("error", done);
          resolve();
        };
        this.audio.addEventListener("ended", done);
        this.audio.addEventListener("error", done);
        this.audio.play().catch((err) => {
          this.report(`voice: playback failed (${err?.message ?? err})`);
          done();
        });
      });
    } catch (err) {
      this.report(`voice: fetch failed (${(err as Error)?.message ?? err})`);
    } finally {
      if (url) {
        URL.revokeObjectURL(url);
        if (this.currentUrl === url) this.currentUrl = null;
      }
    }
  }

  cancel(): void {
    this.audio.pause();
    this.audio.removeAttribute("src");
    this.audio.load();
    if (this.currentUrl) {
      URL.revokeObjectURL(this.currentUrl);
      this.currentUrl = null;
    }
  }
}

// Drains one utterance at a time, awaiting each speak() so they never overlap.
export class SpeechQueue {
  private backend: SpeechBackend;
  private items: string[] = [];
  private draining = false;

  constructor(backend: SpeechBackend) {
    this.backend = backend;
  }

  // True while an utterance is mid-flight. Voice *input* (phrase activation)
  // reads this to mute itself so PAI's own TTS doesn't trip the wake word.
  get speaking(): boolean {
    return this.draining;
  }

  setErrorReporter(reporter: SpeechErrorReporter | null): void {
    // Duck-typed: forwarded to backends that expose `onError` (ServerSpeechBackend
    // does). Keeps SpeechBackend itself minimal.
    (this.backend as { onError?: SpeechErrorReporter | null }).onError = reporter;
  }

  enqueue(text: string): void {
    this.items.push(text);
    if (!this.draining) void this.drain();
  }

  clear(): void {
    this.items = [];
    this.backend.cancel();
  }

  private async drain(): Promise<void> {
    this.draining = true;
    try {
      while (this.items.length) {
        const next = this.items.shift()!;
        await this.backend.speak(next);
      }
    } finally {
      this.draining = false;
    }
  }
}
