// A small circular gauge that fills as a PAI's conversation approaches the
// compaction threshold (ctx_tokens / ctx_limit). Sits at the right edge of the
// composer. Color escalates calm → warn → full as the ring tops out.

const SIZE = 30;
const STROKE = 3;
const R = (SIZE - STROKE) / 2;
const C = 2 * Math.PI * R;

export function ContextRing({ tokens, limit }: { tokens: number; limit: number }) {
  if (!limit || limit <= 0) return null;

  const frac = Math.max(0, Math.min(tokens / limit, 1));
  const pct = Math.round(frac * 100);
  // calm under 75%, warn 75–95%, full at/over 95% (near the auto-compact line).
  const level = frac >= 0.95 ? "full" : frac >= 0.75 ? "warn" : "calm";
  const offset = C * (1 - frac);

  // Full exact counts (e.g. 12,345 / 187,000) for the hover tooltip.
  const used = tokens.toLocaleString();
  const total = limit.toLocaleString();

  return (
    <div
      className={`composer-ctx ${level}`}
      role="img"
      aria-label={`Context ${pct}% — ${used} of ${total} tokens used. PAI auto-compacts when the limit is reached.`}
    >
      <svg width={SIZE} height={SIZE} viewBox={`0 0 ${SIZE} ${SIZE}`} aria-hidden="true">
        <circle
          className="composer-ctx-track"
          cx={SIZE / 2}
          cy={SIZE / 2}
          r={R}
          fill="none"
          strokeWidth={STROKE}
        />
        <circle
          className="composer-ctx-fill"
          cx={SIZE / 2}
          cy={SIZE / 2}
          r={R}
          fill="none"
          strokeWidth={STROKE}
          strokeLinecap="round"
          strokeDasharray={C}
          strokeDashoffset={offset}
          transform={`rotate(-90 ${SIZE / 2} ${SIZE / 2})`}
        />
      </svg>
      <span className="composer-ctx-pct">{pct}</span>
      <div className="composer-ctx-tip" role="tooltip">
        <span className="composer-ctx-tip-count">
          {used} <span className="composer-ctx-tip-sep">/</span> {total}
        </span>
        <span className="composer-ctx-tip-label">tokens used ({pct}%)</span>
        <span className="composer-ctx-tip-note">
          PAI auto-compacts when the limit is reached.
        </span>
      </div>
    </div>
  );
}
