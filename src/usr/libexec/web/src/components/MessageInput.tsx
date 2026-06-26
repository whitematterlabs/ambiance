import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import { Gauge } from "lucide-react";
import { ContextRing } from "./ContextRing";

export function MessageInput({
  disabled,
  onSubmit,
  onInterrupt,
  onTranscribeAudio,
  onVoiceStatus,
  prefill,
  overclockRunning = false,
  ctxTokens = 0,
  ctxLimit = 0,
}: {
  disabled: boolean;
  onSubmit: (text: string, options?: { overclock?: boolean }) => void;
  onInterrupt: () => void;
  onTranscribeAudio: (audio: Blob) => Promise<string>;
  onVoiceStatus: (status: string) => void;
  prefill?: { text: string; nonce: number } | null;
  overclockRunning?: boolean;
  ctxTokens?: number;
  ctxLimit?: number;
}) {
  const [value, setValue] = useState("");
  const [overclockDraft, setOverclockDraft] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const [recordingState, setRecordingState] = useState<"idle" | "recording" | "transcribing">(
    "idle",
  );
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const overclockMode = overclockDraft || overclockRunning;
  const isShell = !overclockMode && value.startsWith("!");
  const voiceBusy = recordingState !== "idle";
  const canRecord =
    typeof navigator !== "undefined" &&
    Boolean(navigator.mediaDevices?.getUserMedia) &&
    typeof MediaRecorder !== "undefined";

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    if (voiceBusy) return;
    const text = value.trim();
    if (!text) return;
    const overclock = overclockDraft;
    setValue("");
    setOverclockDraft(false);
    onSubmit(text, overclock ? { overclock: true } : undefined);
  };

  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "0px";
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
  }, [value]);

  useEffect(() => {
    return () => {
      const recorder = recorderRef.current;
      if (recorder) {
        recorder.onstop = null;
        if (recorder.state !== "inactive") recorder.stop();
      }
      stopStream(streamRef.current);
    };
  }, []);

  // Seed the field from a quick-action (e.g. the Compact button) and drop the
  // caret at the end so the user can keep typing their summary immediately.
  useEffect(() => {
    if (!prefill) return;
    setOverclockDraft(false);
    setValue(prefill.text);
    const el = inputRef.current;
    if (!el) return;
    el.focus();
    requestAnimationFrame(() => el.setSelectionRange(el.value.length, el.value.length));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefill?.nonce]);

  const handleInputKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing) return;
    e.preventDefault();
    submit();
  };

  const handleOverclockClick = () => {
    if (overclockRunning) {
      setOverclockDraft(false);
      onInterrupt();
      return;
    }
    if (disabled || voiceBusy) return;
    setOverclockDraft((v) => !v);
    inputRef.current?.focus();
  };

  const startRecording = async () => {
    if (!canRecord || disabled || recordingState !== "idle") return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
      const mimeType = pickMimeType();
      const recorder = mimeType
        ? new MediaRecorder(stream, { mimeType })
        : new MediaRecorder(stream);
      streamRef.current = stream;
      recorderRef.current = recorder;
      chunksRef.current = [];

      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) chunksRef.current.push(event.data);
      };
      recorder.onstop = () => {
        void finishRecording(recorder);
      };
      recorder.start();
      setRecordingState("recording");
      onVoiceStatus("voice: recording...");
    } catch (e) {
      stopStream(streamRef.current);
      streamRef.current = null;
      recorderRef.current = null;
      setRecordingState("idle");
      onVoiceStatus(`voice: ${messageForError(e)}`);
    }
  };

  const stopRecording = () => {
    const recorder = recorderRef.current;
    if (!recorder || recorder.state === "inactive") return;
    onVoiceStatus("voice: finishing recording...");
    recorder.stop();
  };

  const finishRecording = async (recorder: MediaRecorder) => {
    const chunks = chunksRef.current;
    chunksRef.current = [];
    recorderRef.current = null;
    stopStream(streamRef.current);
    streamRef.current = null;

    if (!chunks.length) {
      setRecordingState("idle");
      onVoiceStatus("voice: no audio captured");
      return;
    }

    const audio = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
    setRecordingState("transcribing");
    onVoiceStatus("voice: transcribing...");
    try {
      const transcript = (await onTranscribeAudio(audio)).trim();
      if (transcript) {
        setValue((prev) => {
          const existing = prev.trim();
          return existing ? `${existing} ${transcript}` : transcript;
        });
        onVoiceStatus("voice: transcript ready");
      } else {
        onVoiceStatus("voice: no speech detected");
      }
    } catch (e) {
      onVoiceStatus(`voice: ${messageForError(e)}`);
    } finally {
      setRecordingState("idle");
    }
  };

  return (
    <form
      className={`composer ${isShell ? "shell" : ""} ${overclockDraft ? "overclock" : ""}`}
      onSubmit={submit}
    >
      <button
        className="composer-stop"
        type="button"
        disabled={disabled}
        onClick={onInterrupt}
        title="Interrupt (Esc)"
        aria-label="Interrupt"
      >
        ◼
      </button>
      <button
        className={`composer-overclock ${overclockMode ? "active" : ""}`}
        type="button"
        disabled={disabled || (!overclockRunning && voiceBusy)}
        onClick={handleOverclockClick}
        aria-pressed={overclockMode}
        title={overclockRunning ? "Stop Overclock" : "Overclock mode"}
        aria-label={overclockRunning ? "Stop Overclock" : "Overclock mode"}
      >
        <Gauge aria-hidden="true" focusable="false" />
      </button>
      <div className="composer-field">
        {overclockDraft && (
          <div className="composer-overclock-tab">
            <span className="composer-overclock-tab-label">Overclocked</span>
            <span className="composer-overclock-tab-desc">
              PAI will continue working until a specific condition is fulfilled.
            </span>
          </div>
        )}
        <textarea
          ref={inputRef}
          className="composer-input"
          rows={1}
          placeholder={
            disabled
              ? "No active PAI"
              : overclockDraft
                ? "Keep working until you find a great deal on Honolulu hotels"
                : "Message your PAI...  (start with ! for shell)"
          }
          value={value}
          disabled={disabled || recordingState === "transcribing"}
          autoFocus
          enterKeyHint="send"
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleInputKeyDown}
        />
      </div>
      {isShell && <span className="composer-tag">shell</span>}
      <button
        className={`composer-mic ${recordingState}`}
        type="button"
        disabled={disabled || !canRecord || recordingState === "transcribing"}
        onClick={recordingState === "recording" ? stopRecording : startRecording}
        aria-pressed={recordingState === "recording"}
        title={
          canRecord
            ? recordingState === "recording"
              ? "Stop recording"
              : recordingState === "transcribing"
                ? "Transcribing"
                : "Record voice input"
            : "Voice input unavailable in this browser"
        }
        aria-label={
          recordingState === "recording"
            ? "Stop recording"
            : recordingState === "transcribing"
              ? "Transcribing"
              : "Record voice input"
        }
      >
        <MicIcon />
      </button>
      {!disabled && ctxLimit > 0 && <ContextRing tokens={ctxTokens} limit={ctxLimit} />}
      <button
        className="composer-send"
        type="submit"
        disabled={disabled || voiceBusy || !value.trim()}
        title="Send"
        aria-label="Send"
      >
        ↑
      </button>
    </form>
  );
}

function pickMimeType(): string | undefined {
  if (typeof MediaRecorder === "undefined" || !MediaRecorder.isTypeSupported) return undefined;
  const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"];
  return candidates.find((type) => MediaRecorder.isTypeSupported(type));
}

function stopStream(stream: MediaStream | null) {
  stream?.getTracks().forEach((track) => track.stop());
}

function messageForError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function MicIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M12 14.5a3 3 0 0 0 3-3v-5a3 3 0 0 0-6 0v5a3 3 0 0 0 3 3Z" />
      <path d="M18 11.5a6 6 0 0 1-12 0" />
      <path d="M12 17.5v3" />
      <path d="M8.5 20.5h7" />
    </svg>
  );
}
