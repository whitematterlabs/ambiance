import SwiftUI

struct MenuView: View {
    @ObservedObject var registry: PAIRegistry
    var openChat: (PAIInfo) -> Void

    var body: some View {
        if !registry.kernelOnline {
            Text("kernel offline")
                .foregroundStyle(.secondary)
            Divider()
            Text("Start with: cd ~/.pai && usr/bin/python -m boot run")
                .font(.caption)
                .foregroundStyle(.tertiary)
        } else if registry.pais.isEmpty {
            Text("no running PAIs")
                .foregroundStyle(.secondary)
        } else {
            ForEach(registry.pais) { pai in
                Button("\(pai.slug) #\(pai.pid) — \(pai.description)") {
                    openChat(pai)
                }
            }
        }
        Divider()
        Button("Quit PAI") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }
}
