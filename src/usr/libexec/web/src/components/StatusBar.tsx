export function StatusBar({ text }: { text: string }) {
  return (
    <div className="status-bar">
      <span>{text}</span>
    </div>
  );
}
