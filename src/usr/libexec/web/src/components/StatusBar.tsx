export function StatusBar({ text, recording = false }: { text: string; recording?: boolean }) {
  return (
    <div className="status-bar">
      <span>{text}</span>
      {recording && (
        <span className="recording-pill" title="Notetaker is recording this call">
          ● recording
        </span>
      )}
    </div>
  );
}
