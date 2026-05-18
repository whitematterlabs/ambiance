import SwiftUI

@main
struct PAIApp: App {
    @StateObject private var registry = PAIRegistry()

    var body: some Scene {
        MenuBarExtra {
            // Routed through a View so we can use the openWindow
            // environment value, which is unavailable at App scope.
            MenuRoot(registry: registry)
        } label: {
            if registry.kernelOnline {
                Text("P·\(registry.pais.count)")
            } else {
                Text("P·!").foregroundStyle(.red)
            }
        }
        .menuBarExtraStyle(.menu)

        // One window per pid. Opened from MenuRoot via openWindow(value:).
        WindowGroup(for: Int.self) { $pid in
            if let pid {
                ChatWindowHost(pid: pid, registry: registry)
            } else {
                Text("No PAI selected.").padding()
            }
        }
        .windowResizability(.contentMinSize)

        Settings { Text("PAI — MVP").padding() }
    }

    init() {
        registry.start()
    }
}

private struct MenuRoot: View {
    @ObservedObject var registry: PAIRegistry
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        MenuView(registry: registry) { pai in
            NSApp.activate(ignoringOtherApps: true)
            openWindow(value: pai.pid)
        }
    }
}

/// Bridges a pid back to the live PAIInfo (which carries slug/description).
private struct ChatWindowHost: View {
    let pid: Int
    @ObservedObject var registry: PAIRegistry

    var body: some View {
        if let pai = registry.pais.first(where: { $0.pid == pid }) {
            ChatWindow(pai: pai)
        } else {
            // PAI vanished (kernel restarted, paictl stopped it) — keep the
            // window open with a placeholder rather than crashing.
            Text("PAI #\(pid) is not running.")
                .frame(minWidth: 360, minHeight: 180)
        }
    }
}
