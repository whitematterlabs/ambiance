import { useEffect, useRef, useState, type FormEvent } from "react";

export function MessageInput({
  disabled,
  onSubmit,
  onInterrupt,
  onTranscribeAudio,
  onVoiceStatus,
}: {
  disabled: boolean;
  onSubmit: (text: string) => void;
  onInterrupt: () => void;
  onTranscribeAudio: (audio: Blob) => Promise<string>;
  onVoiceStatus: (status: string) => void;
}) {
  const [value, setValue] = useState("");
  const [recordingState, setRecordingState] = useState<"idle" | "recording" | "transcribing">(
    "idle",
  );
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const isShell = value.startsWith("!");
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
    setValue("");
    onSubmit(text);
  };

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
    <form className={`composer ${isShell ? "shell" : ""}`} onSubmit={submit}>
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
      <input
        className="composer-input"
        placeholder={
          disabled ? "No active PAI" : "Message your PAI...  (start with ! for shell)"
        }
        value={value}
        disabled={disabled || recordingState === "transcribing"}
        autoFocus
        onChange={(e) => setValue(e.target.value)}
      />
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
