import SwiftUI

/// Sidebar for the unified main window. Three groups:
///   1. Overview — Activity + Processes (always present)
///   2. PAIs     — one row per running PAI, with status dot
struct Sidebar: View {
    @ObservedObject var registry: PAIRegistry
    @ObservedObject var procs: ProcRegistry
    @ObservedObject var cloner: PAICloner
    @Binding var selection: AppSelection?
    @State private var hoveredPID: Int? = nil

    var body: some View {
        List(selection: $selection) {
            Section("Overview") {
                Label("Activity", systemImage: "waveform")
                    .tag(AppSelection.activity)
                Label {
                    HStack {
                        Text("Processes")
                        Spacer()
                        Text("\(procs.rows.count)")
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 1)
                            .background(
                                Capsule().fill(Color.secondary.opacity(0.15))
                            )
                    }
                } icon: {
                    Image(systemName: "list.bullet.rectangle")
                }
                .tag(AppSelection.procs)
            }

            Section("PAIs") {
                if registry.pais.isEmpty {
                    Text(registry.kernelOnline ? "no running PAIs" : "kernel offline")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .padding(.vertical, 2)
                } else {
                    ForEach(registry.pais) { pai in
                        paiRow(pai).tag(AppSelection.pai(pai.pid))
                    }
                }
            }
        }
        .listStyle(.sidebar)
        .frame(minWidth: 200, idealWidth: 230)
    }

    private func paiRow(_ pai: PAIInfo) -> some View {
        let color = palette(for: pai.pid)
        let cloning = cloner.inFlight.contains(pai.slug)
        let hovered = hoveredPID == pai.pid
        return Label {
            HStack(spacing: 6) {
                Text(pai.slug)
                    .lineLimit(1)
                if let b = pai.busy {
                    Text(busyTrailing(b))
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.orange)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1)
                        .background(
                            Capsule().fill(Color.orange.opacity(0.15))
                        )
                        .lineLimit(1)
                }
                Spacer(minLength: 4)
                Text("#\(pai.pid)")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.tertiary)
                    .opacity(hovered ? 0 : 1)
                Button {
                    cloner.clone(source: pai.slug)
                } label: {
                    if cloning {
                        ProgressView().controlSize(.mini)
                    } else {
                        Image(systemName: "plus.circle.fill")
                            .symbolRenderingMode(.hierarchical)
                            .foregroundStyle(hovered ? Color.accentColor : .secondary)
                    }
                }
                .buttonStyle(.borderless)
                .help("Clone this PAI")
                .disabled(cloning)
            }
        } icon: {
            ZStack {
                if pai.busy != nil {
                    Circle()
                        .stroke(Color.orange.opacity(0.35), lineWidth: 2)
                        .frame(width: 14, height: 14)
                }
                Circle().fill(color).frame(width: 10, height: 10)
                    .shadow(color: color.opacity(0.4), radius: 2, x: 0, y: 0)
            }
            .frame(width: 16, height: 16)
        }
        .contentShape(Rectangle())
        .onHover { inside in
            hoveredPID = inside ? pai.pid : (hoveredPID == pai.pid ? nil : hoveredPID)
        }
        .contextMenu {
            Button("Clone \(pai.slug)") { cloner.clone(source: pai.slug) }
                .disabled(cloning)
        }
    }

    private func busyTrailing(_ b: BusyState) -> String {
        if let e = b.elapsed { return "\(Int(e))s" }
        return "busy"
    }
}
