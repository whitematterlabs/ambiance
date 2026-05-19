import SwiftUI

/// Live tail of `~/.pai/var/log/kernel/kernel.log`. Mirrors the TUI's
/// ActivityLog widget (widgets.py:184-271) — same line-classification,
/// same color cues, same elision rules — but rendered as a SwiftUI
/// scrolling list so the OS handles offscreen reuse.
struct ActivityWindow: View {
    @ObservedObject var log: KernelLogTailer

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(log.lines.enumerated()), id: \.element.id) { idx, line in
                        row(line)
                            .padding(.horizontal, 14)
                            .padding(.vertical, 2)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(
                                idx.isMultiple(of: 2)
                                    ? Color.clear
                                    : Color.primary.opacity(0.03)
                            )
                            .id(line.id)
                    }
                }
                .padding(.vertical, 6)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .onChange(of: log.lines) { _, new in
                if let last = new.last {
                    proxy.scrollTo(last.id, anchor: .bottom)
                }
            }
        }
        .frame(minWidth: 640, minHeight: 360)
        .background(Color(nsColor: .textBackgroundColor))
        .navigationTitle("PAI — activity")
    }

    private var logFont: Font { .system(size: 11.5, design: .monospaced) }

    @ViewBuilder
    private func row(_ line: ActivityLine) -> some View {
        switch line.kind {
        case .nudgeStart:
            Text("> ") .foregroundStyle(.yellow).fontWeight(.bold)
                + Text(strip("[kernel] ", line.raw)).foregroundStyle(.yellow)
        case .nudgeFail:
            Text("! ").foregroundStyle(.red).fontWeight(.bold)
                + Text(strip("[kernel] ", line.raw)).foregroundStyle(.red)
        case .nudgeDone:
            Text("  done.").foregroundStyle(.green).opacity(0.7)
        case .paiCommand:
            Text(line.raw).foregroundStyle(.cyan).fontDesign(.monospaced)
        case .paiSay:
            Text(line.raw).foregroundStyle(.purple).fontDesign(.monospaced)
        case .commandOutput:
            Text("    " + line.raw.trimmingCharacters(in: .whitespaces))
                .foregroundStyle(.secondary)
                .fontDesign(.monospaced)
                .lineLimit(1)
                .truncationMode(.tail)
        case .commandExit(let ok, let code):
            Text("    \(ok ? "ok" : "fail") (exit \(code))")
                .foregroundStyle(ok ? .green : .red)
                .fontDesign(.monospaced)
        case .other:
            Text(line.raw)
                .foregroundStyle(.tertiary)
                .fontDesign(.monospaced)
        }
    }

    private func strip(_ prefix: String, _ s: String) -> String {
        s.hasPrefix(prefix) ? String(s.dropFirst(prefix.count)) : s
    }
}
