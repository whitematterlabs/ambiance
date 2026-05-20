import Foundation
import Combine

/// One event seen flying through `~/.pai/run/pai/events/`. Mirrors the
/// TUI's EventSighting (src/sbin/tui/state.py:237-245) and the way the
/// EventStrip widget formats them (src/sbin/tui/widgets.py:126-158).
struct EventSighting: Identifiable, Hashable {
    let id = UUID()
    let at: Date
    let filename: String
    let source: String
    let kind: String   // "source:kind" form, for display
    let target: String // thread / handle / slug — whatever the payload had
    let consumed: Bool // true if the kernel unlinked it before we could read
}

/// Watches `$PAI_ROOT/run/pai/events/` and publishes each new YAML drop
/// as an `EventSighting`. Like the TUI, we race the kernel: try to read
/// the file in our event handler; if the kernel already consumed it,
/// fall back to what the filename (`{ts}-{source}.yaml`) tells us.
///
/// Uses `DispatchSource.makeFileSystemObjectSource` on a fd opened with
/// `O_EVTONLY` — the macOS-native cousin of the watchdog observer the
/// TUI uses. Cheap (one event per directory mutation, fires on the main
/// queue) and avoids the polling lag of size-stat tailers.
@MainActor
final class EventsTailer: ObservableObject {
    @Published private(set) var sightings: [EventSighting] = []

    private let dir: URL = FHS.eventsDir
    private var source: DispatchSourceFileSystemObject?
    private var fd: CInt = -1
    /// Filenames we've already surfaced. Includes both currently-on-disk
    /// entries and recently-consumed ones, so a delete+recreate of the
    /// same name (unlikely — names embed microseconds) won't double-emit.
    private var announced: Set<String> = []
    private let maxBuffer = 200

    func start() {
        try? FileManager.default.createDirectory(
            at: dir, withIntermediateDirectories: true
        )
        // Backlog-suppression: anything already on disk at launch is
        // treated as already-seen so we don't replay a queue.
        if let existing = try? FileManager.default.contentsOfDirectory(atPath: dir.path) {
            announced = Set(existing.filter { isEventFile($0) })
        }
        fd = open(dir.path, O_EVTONLY)
        guard fd >= 0 else { return }
        let src = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd,
            eventMask: [.write, .extend, .rename, .delete],
            queue: .main
        )
        src.setEventHandler { [weak self] in self?.scan() }
        src.setCancelHandler { [weak self] in
            if let fd = self?.fd, fd >= 0 { close(fd) }
            self?.fd = -1
        }
        src.resume()
        source = src
    }

    func stop() {
        source?.cancel()
        source = nil
    }

    private func isEventFile(_ name: String) -> Bool {
        name.hasSuffix(".yaml") && !name.hasPrefix(".")
    }

    private func scan() {
        // The kernel may have read+unlinked some entries before we got
        // here. We still want to surface that something happened, so
        // diff against the dir listing AND keep entries we briefly saw
        // even if they're gone now.
        let current: Set<String>
        if let entries = try? FileManager.default.contentsOfDirectory(atPath: dir.path) {
            current = Set(entries.filter(isEventFile))
        } else {
            current = []
        }
        let fresh = current.subtracting(announced)
        for name in fresh.sorted() {
            announce(filename: name)
        }
    }

    private func announce(filename: String) {
        announced.insert(filename)
        let url = dir.appendingPathComponent(filename)
        let yaml = (try? Data(contentsOf: url)).flatMap { parseFlatYaml($0) }
        let consumed = yaml == nil
        let payload = yaml ?? [:]
        let src = payload["source"] ?? sourceFromFilename(filename)
        let kindRaw = payload["kind"] ?? (consumed ? "(consumed)" : "?")
        let label = kindRaw.hasPrefix("\(src):") ? kindRaw : "\(src):\(kindRaw)"
        let target = payload["thread"]
            ?? payload["handle"]
            ?? payload["slug"]
            ?? ""
        let sighting = EventSighting(
            at: Date(),
            filename: filename,
            source: src,
            kind: label,
            target: target,
            consumed: consumed
        )
        sightings.append(sighting)
        if sightings.count > maxBuffer {
            sightings.removeFirst(sightings.count - maxBuffer)
        }
    }

    /// Filename is `{ts}-{source}.yaml`. The timestamp itself contains
    /// hyphens (date + microseconds), so we want the *last* hyphen.
    private func sourceFromFilename(_ name: String) -> String {
        let stem = name.hasSuffix(".yaml")
            ? String(name.dropLast(5))
            : name
        guard let dash = stem.lastIndex(of: "-") else { return "?" }
        return String(stem[stem.index(after: dash)...])
    }

    /// Minimal flat-YAML extractor. Events written by `emit_event` are
    /// shallow `key: value` records; we don't need full YAML semantics,
    /// just the handful of fields the strip displays (source/kind/thread/
    /// handle/slug). Strips quotes and surrounding whitespace.
    private func parseFlatYaml(_ data: Data) -> [String: String]? {
        guard let text = String(data: data, encoding: .utf8) else { return nil }
        var out: [String: String] = [:]
        for raw in text.split(separator: "\n", omittingEmptySubsequences: true) {
            let line = String(raw)
            // Skip nested-mapping markers, comments, etc — we only care
            // about top-level scalar keys.
            if line.first == " " || line.first == "\t" { continue }
            if line.hasPrefix("#") { continue }
            guard let colon = line.firstIndex(of: ":") else { continue }
            let key = String(line[..<colon]).trimmingCharacters(in: .whitespaces)
            var val = String(line[line.index(after: colon)...])
                .trimmingCharacters(in: .whitespaces)
            if (val.hasPrefix("\"") && val.hasSuffix("\"") && val.count >= 2)
                || (val.hasPrefix("'") && val.hasSuffix("'") && val.count >= 2) {
                val = String(val.dropFirst().dropLast())
            }
            if !key.isEmpty { out[key] = val }
        }
        return out
    }
}
