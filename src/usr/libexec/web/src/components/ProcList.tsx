import type { CSSProperties } from "react";
import type { ProcRow } from "../types";
import { paiColor } from "../palette";

// Compact token count: '-' if zero, else 12.3k / 187k / 1.2M (matches _fmt_ctx).
function fmtCtx(n: number): string {
  if (!n) return "-";
  if (n < 1000) return String(n);
  if (n < 10_000) return `${(n / 1000).toFixed(1)}k`;
  if (n < 1_000_000) return `${Math.floor(n / 1000)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

export function ProcList({ rows }: { rows: ProcRow[] }) {
  return (
    <div className="proc-list">
      <table>
        <thead>
          <tr>
            <th>slug</th>
            <th>pid</th>
            <th>type</th>
            <th>parent</th>
            <th>ctx</th>
            <th>when</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr className="empty-row">
              <td colSpan={6}>no running processes</td>
            </tr>
          )}
          {rows.map((r) => {
            const paiLike = r.type === "pai";
            const style = paiLike
              ? ({ "--pai-color": paiColor(r.slug || r.pid) } as CSSProperties)
              : undefined;
            return (
              <tr
                key={r.slug}
                className={`${r.busy ? "busy" : ""} ${paiLike ? "pai-coded" : ""}`}
                style={style}
              >
                <td className="slug">
                  {paiLike && <span className="proc-color-dot" aria-hidden="true" />}
                  <span className="tree">{r.tree_prefix}</span>
                  {r.slug}
                </td>
                <td>{r.pid || "-"}</td>
                <td className={`ptype ptype-${r.type}`}>{r.type}</td>
                <td>{r.parent || "-"}</td>
                <td>{fmtCtx(r.ctx_tokens)}</td>
                <td className="when">{r.when_short}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
