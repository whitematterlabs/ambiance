import { useEffect, useRef } from "react";

// Colour by speaker prefix, matching LogTail.write_line.
function lineClass(line: string): string {
  if (line.startsWith("[kernel]")) return "log-kernel";
  if (/^\[pai(:[^\]]+)?\]/.test(line)) return "log-pai";
  return "";
}

export function LogTail({ lines }: { lines: string[] }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines]);

  return (
    <div className="log-tail scroll" ref={ref}>
      {lines.length === 0 && <div className="feed-empty">waiting for kernel.log…</div>}
      {lines.map((l, i) => (
        <div key={i} className={`log-line ${lineClass(l)}`}>
          {l}
        </div>
      ))}
    </div>
  );
}
