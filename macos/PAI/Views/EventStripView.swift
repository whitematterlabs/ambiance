import SwiftUI

/// Right-side companion to ChatWindow: a live list of events flowing
/// through `~/.pai/run/pai/events/`. Mirrors the TUI's EventStrip
/// (src/sbin/tui/widgets.py:126-158) — same `HH:MM:SS  source:kind  → target`
/// shape, same color cues (timestamp dim, kind yellow, target plain,
/// consumed events de-emphasized).
struct EventStripView: View {
    @ObservedObject var tailer: EventsTailer

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider().opacity(0.4)
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 2) {
                        if tailer.sightings.isEmpty {
                            Text("waiting for events…")
                                .font(.caption)
                                .foregroundStyle(.tertiary)
                                .padding(.horizontal, 10)
                                .padding(.top, 10)
                        }
                        ForEach(tailer.sightings) { s in
                            row(s)
                                .padding(.horizontal, 10)
                                .padding(.vertical, 2)
                                .id(s.id)
                        }
                    }
                    .padding(.vertical, 6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .onChange(of: tailer.sightings) { _, new in
                    if let last = new.last {
                        proxy.scrollTo(last.id, anchor: .bottom)
                    }
                }
            }
        }
        .frame(minWidth: 220)
        .background(Color(nsColor: .underPageBackgroundColor))
    }

    private var header: some View {
        HStack {
            Text("events")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            Spacer()
            Text("\(tailer.sightings.count)")
                .font(.caption2.monospacedDigit())
                .foregroundStyle(.tertiary)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(.regularMaterial)
    }

    private func row(_ s: EventSighting) -> some View {
        let stamp = Self.stampFormatter.string(from: s.at)
        var line = Text(stamp + " ").foregroundStyle(.tertiary)
            + Text(s.kind).foregroundStyle(s.consumed ? Color.secondary : .yellow)
        if !s.target.isEmpty {
            line = line
                + Text(" → ").foregroundStyle(.tertiary)
                + Text(s.target).foregroundStyle(.primary)
        }
        return line
            .font(.system(size: 11, design: .monospaced))
            .lineLimit(2)
            .truncationMode(.tail)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private static let stampFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "HH:mm:ss"
        return f
    }()
}
