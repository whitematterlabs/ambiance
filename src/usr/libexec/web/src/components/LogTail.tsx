import type { CSSProperties } from "react";
import { useEffect, useRef } from "react";
import { paiColor } from "../palette";

const PAI_PREFIX = /^\[pai(?::([^\]]+))?\]/;

// Colour by speaker prefix, matching LogTail.write_line.
function lineClass(line: string): string {
  if (line.startsWith("[kernel]")) return "log-kernel";
  if (PAI_PREFIX.test(line)) return "log-pai";
  return "";
}

function paiSlug(line: string): string | null {
  const m = PAI_PREFIX.exec(line);
  return m?.[1] || null;
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
      {lines.map((l, i) => {
        const slug = paiSlug(l);
        const style = slug ? ({ "--pai-color": paiColor(slug) } as CSSProperties) : undefined;
        return (
          <div
            key={i}
            className={`log-line ${lineClass(l)} ${slug ? "pai-coded" : ""}`}
            style={style}
          >
            {l}
          </div>
        );
      })}
    </div>
  );
}
