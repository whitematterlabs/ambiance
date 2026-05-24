// Per-PAI accent colors. Deliberately muted, warm earth tones that all sit in
// the same desaturated family as the clay accent — they read as "ink with a
// hue," never as candy-bright status dots. Used only as thin edge accents
// (a left rule on tabs/rows), never as filled circles.
const PAI_COLORS = [
  "#b5683d", // terracotta
  "#7c8a5a", // olive
  "#a07c3a", // ochre
  "#9a5b4c", // brick
  "#5f7a6e", // pine
  "#86638c", // muted plum
  "#3f6f7d", // slate teal
  "#a86b48", // pecan
  "#6b7a4f", // moss
  "#94604e", // sienna
  "#577a8a", // dusty blue
  "#8a6f3c", // bronze
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
