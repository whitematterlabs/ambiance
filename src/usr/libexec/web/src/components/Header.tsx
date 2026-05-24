export function Header({
  connected,
  kernelRunning,
  kernelBusy,
  onToggleKernel,
  voiceEnabled,
  onToggleVoice,
}: {
  connected: boolean;
  kernelRunning: boolean;
  kernelBusy: boolean;
  onToggleKernel: () => void;
  voiceEnabled: boolean;
  onToggleVoice: () => void;
}) {
  return (
    <header className="header">
      <div className="brand">
        <span className="brand-name">PAI</span>
        <span
          className={`conn-status ${connected ? "on" : "off"}`}
          role="status"
          aria-label={connected ? "Connected" : "Disconnected"}
          title={connected ? "Connected" : "Disconnected"}
        >
          {connected ? "Online" : "Offline"}
        </span>
        <button
          className="kernel-toggle"
          type="button"
          disabled={!connected || kernelBusy}
          onClick={onToggleKernel}
          title={kernelRunning ? "Stop kernel" : "Start kernel"}
        >
          {kernelBusy ? "Kernel..." : kernelRunning ? "Stop kernel" : "Start kernel"}
        </button>
      </div>
      <span className="spacer" />
      <button
        className="ghost-button"
        type="button"
        onClick={onToggleVoice}
        aria-pressed={voiceEnabled}
        title={voiceEnabled ? "Voice on — reading replies aloud" : "Voice off"}
      >
        <span className="ghost-label">{voiceEnabled ? "Voice on" : "Voice off"}</span>
      </button>
    </header>
  );
}
