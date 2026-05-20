import SwiftUI

/// Thin strip pinned to the bottom of the main window. Mirrors the TUI's
/// `#status` footer (`src/sbin/tui/app.py:341-350`): when a PAI is
/// selected, show "slug: reason (Ns)" while busy, or just "slug: idle".
/// For Activity / Processes, show a fleet aggregate instead.
struct StatusBar: View {
    @ObservedObject var registry: PAIRegistry
    @ObservedObject var launcher: KernelLauncher
    let selection: AppSelection?

    var body: some View {
        HStack(spacing: 8) {
            kernelMenu
            Text(text)
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.tail)
            Spacer(minLength: 8)
            Text(fleetSummary)
                .font(.caption.monospacedDigit())
                .foregroundStyle(.tertiary)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 4)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.thinMaterial)
    }

    private var kernelMenu: some View {
        Menu {
            if registry.kernelOnline {
                Button("Stop kernel") { launcher.stop() }
                    .disabled(launcher.inFlight)
            } else {
                Button("Start kernel") { launcher.start() }
                    .disabled(launcher.inFlight)
            }
        } label: {
            HStack(spacing: 5) {
                Circle()
                    .fill(registry.kernelOnline ? Color.green : Color.red)
                    .frame(width: 7, height: 7)
                Text(registry.kernelOnline ? "kernel" : "offline")
                    .font(.caption.monospaced())
            }
        }
        .menuStyle(.button)
        .controlSize(.small)
        .fixedSize()
        .help(registry.kernelOnline ? "Kernel online — click for actions" : "Kernel offline — click to start")
    }

    private var text: String {
        guard registry.kernelOnline else { return "kernel offline" }
        switch selection {
        case .pai(let pid):
            guard let pai = registry.pais.first(where: { $0.pid == pid }) else {
                return "—"
            }
            if let b = pai.busy {
                if let e = b.elapsed {
                    return "\(pai.slug): \(b.reason) (\(Int(e))s)"
                }
                return "\(pai.slug): \(b.reason)"
            }
            return "\(pai.slug): idle"
        case .activity:
            return "activity — live tail of var/log/kernel/kernel.log"
        case .procs:
            return "processes — all /proc/<slug>/"
        case .none:
            return "idle"
        }
    }

    private var fleetSummary: String {
        let total = registry.pais.count
        let busy = registry.pais.filter { $0.busy != nil }.count
        if busy > 0 { return "\(busy)/\(total) busy" }
        return "\(total) PAIs"
    }
}
