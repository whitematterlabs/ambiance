// Per-PAI accent colors, sampled across the White Matter Labs spectrum (warm
// orange → cool azure) but held a step back from full saturation so they read
// as "ink with a hue," never candy-bright status dots. Used only as thin edge
// accents (a left rule on tabs/rows), never as filled circles.
const PAI_COLORS = [
  "#d2571e", // flare
  "#e08a2c", // amber
  "#d81f6a", // rose
  "#b5279f", // magenta
  "#8a3bc4", // violet
  "#6a1fc0", // purple
  "#3a44b0", // indigo
  "#1f63c9", // azure
  "#c0397a", // pink
  "#7d5bd0", // lilac
  "#2f6fd0", // blue
  "#a34bb8", // orchid
];

export function paiColor(seed: string | number | null | undefined): string {
  const key = String(seed ?? "pai");
  let hash = 2166136261;
  for (let i = 0; i < key.length; i += 1) {
    hash ^= key.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return PAI_COLORS[(hash >>> 0) % PAI_COLORS.length];
}
