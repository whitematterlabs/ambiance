// Plain text "Ambiance" wordmark. Inherits the header ink via `color`
// (currentColor equivalent for text) so it adapts to light/dark.
export function Logo({ className }: { className?: string }) {
  return (
    <span className={className} role="img" aria-label="Ambiance">
      Ambiance
    </span>
  );
}
