import type { BuildStatus } from "../types";

/**
 * Surfaces build skew between the kernel and this web console.
 *
 * The hub auto-reboots a stale kernel on its own, so `kernel_stale` is normally
 * a brief, informational "healing in progress" strip. It turns into a hard
 * warning only when `escalated` (the auto-reboot didn't take). A stale *console*
 * can't be self-healed — it asks the owner to restart the web process.
 */
export function BuildBanner({ build }: { build: BuildStatus | null }) {
  if (!build) return null;
  const { state, kernel, console: consoleVer, current, escalated } = build;
  if (state === "in_sync" || state === "unknown") return null;

  let tone: "info" | "warn" = "info";
  let text = "";

  if (state === "console_stale") {
    tone = "warn";
    text = `This console is on an old build (${consoleVer}); the system is on ${current}. Restart it: \`pai start\`.`;
  } else if (escalated) {
    tone = "warn";
    text = `Kernel is stuck on an old build (${kernel ?? "?"}); auto-reboot didn't take. Run \`sbin/reboot\`.`;
  } else if (state === "kernel_stale" || state === "both_stale") {
    tone = "info";
    text = `Kernel was on an old build (${kernel ?? "?"}) — rebooting into ${current}…`;
  } else {
    return null;
  }

  return (
    <div className={`build-banner build-banner--${tone}`} role="status" aria-live="polite">
      {text}
    </div>
  );
}
