const PAI_COLORS = [
  "#2563eb",
  "#059669",
  "#d97706",
  "#db2777",
  "#7c3aed",
  "#0f766e",
  "#4f46e5",
  "#16a34a",
  "#dc2626",
  "#ca8a04",
  "#0891b2",
  "#9333ea",
  "#ea580c",
  "#be123c",
  "#15803d",
  "#0369a1",
  "#a16207",
  "#c026d3",
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
