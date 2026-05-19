import SwiftUI

/// Lists every entry under `~/.pai/proc/`, not just PAIs. Mirrors the
/// TUI ProcList (widgets.py:82-111) but renders as a SwiftUI table so the
/// columns sort and resize for free.
struct ProcsWindow: View {
    @ObservedObject var procs: ProcRegistry
    @State private var sortOrder: [KeyPathComparator<ProcRow>] = [
        .init(\.treeOrder, order: .forward)
    ]

    var body: some View {
        VStack(spacing: 0) {
            Table(of: ProcRow.self, sortOrder: $sortOrder) {
                TableColumn("status") { row in
                    HStack(spacing: 6) {
                        Circle().fill(statusColor(row)).frame(width: 7, height: 7)
                        Text(row.status.isEmpty ? "—" : row.status)
                            .font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                    }
                }
                .width(min: 90, ideal: 100)

                TableColumn("slug", value: \.slug) { row in
                    HStack(spacing: 0) {
                        if !row.treePrefix.isEmpty {
                            Text(row.treePrefix)
                                .font(.body.monospaced())
                                .foregroundStyle(.tertiary)
                        }
                        Text(row.slug).font(.body.monospaced())
                    }
                }
                .width(min: 140, ideal: 200)

                TableColumn("kind", value: \.kind) { row in
                    Text(row.kind.isEmpty ? "—" : row.kind)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .width(min: 60, ideal: 80)

                TableColumn("pid") { row in
                    Text(row.pid.map(String.init) ?? "—")
                        .font(.caption.monospacedDigit())
                }
                .width(min: 40, ideal: 50)

                TableColumn("parent") { row in
                    Text(row.parent.map(String.init) ?? "—")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                .width(min: 50, ideal: 60)

                TableColumn("busy") { row in
                    if let b = row.busy {
                        HStack(spacing: 4) {
                            Circle().fill(.orange).frame(width: 6, height: 6)
                            Text(busyLabel(b))
                                .font(.caption)
                                .lineLimit(1)
                        }
                    } else {
                        Text("—").foregroundStyle(.tertiary).font(.caption)
                    }
                }
                .width(min: 120, ideal: 200)

                TableColumn("ctx", value: \.ctxTokens) { row in
                    Text(formatCtx(row.ctxTokens))
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(row.ctxTokens > 0 ? .primary : .tertiary)
                }
                .width(min: 50, ideal: 60)

                TableColumn("when", value: \.when) { row in
                    Text(row.when)
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                }
                .width(min: 80, ideal: 140)

                TableColumn("description", value: \.description) { row in
                    Text(row.description)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                .width(min: 120, ideal: 240)
            } rows: {
                ForEach(sortedRows) { row in TableRow(row) }
            }
        }
        .frame(minWidth: 700, minHeight: 360)
        .navigationTitle("PAI — processes")
    }

    private var sortedRows: [ProcRow] {
        procs.rows.sorted(using: sortOrder)
    }

    private func statusColor(_ r: ProcRow) -> Color {
        if r.busy != nil { return .orange }
        switch r.status {
        case "running":   return .green
        case "failed":    return .red
        case "completed": return .gray
        default:          return .secondary
        }
    }

    private func busyLabel(_ b: BusyState) -> String {
        var s = b.reason.isEmpty ? "busy" : b.reason
        if let e = b.elapsed {
            s += "  \(Int(e))s"
        }
        return s
    }
}
