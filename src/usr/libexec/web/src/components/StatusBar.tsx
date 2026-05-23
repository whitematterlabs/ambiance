export function StatusBar({ text }: { text: string }) {
  return (
    <div className="status-bar">
      <span className="status-dot" />
      <span>{text}</span>
    </div>
  );
}
