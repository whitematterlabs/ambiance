// Per-PAI accent shades — a mid-gray ramp, no hue. Kept around 50% luminance so
// each thin edge rule (a left rule on tabs/rows) stays legible on both light
// paper and dark, while still giving fleet members a subtly distinct shade.
const PAI_COLORS = [
  "#8a8a90",
  "#75757b",
  "#9a9aa0",
  "#6f6f75",
  "#a0a0a6",
  "#808086",
  "#95959b",
  "#6a6a70",
  "#8f8f95",
  "#7a7a80",
  "#a5a5ab",
  "#858589",
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
