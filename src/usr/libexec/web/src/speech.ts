// Voice mode's frontend swap point: a speech backend behind a small interface,
// plus a sequential queue so utterances never overlap. Swapping engines later
// (an audio LLM, the browser's SpeechSynthesis) means writing another
// SpeechBackend and constructing the queue with it — the queue, the toggle, and
// the watermark logic in App.tsx never change.

export interface SpeechBackend {
  speak(text: string): Promise<void>; // resolves when audio finishes
  cancel(): void; // stop current + drop in-flight
}

// v1 backend: POST text to the local /api/tts proxy (which holds the ElevenLabs
// key), play the returned mp3 through a single reused <audio> element.
export class ElevenLabsBackend implements SpeechBackend {
  private audio: HTMLAudioElement;
  private currentUrl: string | null = null;

  constructor() {
    this.audio = new Audio();
  }

  async speak(text: string): Promise<void> {
    let url: string | null = null;
    try {
      const res = await fetch("/api/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!res.ok) {
        console.warn(`tts: server returned ${res.status}`);
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
          console.warn("tts: playback failed", err);
          done();
        });
      });
    } catch (err) {
      console.warn("tts: fetch failed", err);
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
