import Foundation
import Combine

/// One categorized line of `~/.pai/var/log/kernel/kernel.log`. The classifier
/// mirrors `src/sbin/tui/widgets.py:205-270`: the kernel emits free-form text,
/// and the TUI's ActivityLog widget picks out a handful of well-known
/// prefixes to color. We do the same here so the UI can render colors.
struct ActivityLine: Identifiable, Hashable {
    enum Kind: Hashable {
        case nudgeStart       // [kernel] nudge: …
        case nudgeFail        // [kernel] nudge failed …
        case nudgeDone        // [kernel] nudge complete
        case paiCommand       // [pai:<pid>] $ <cmd>
        case paiSay           // [pai:<pid>] <rest>
        case commandOutput    // captured stdout/stderr between $ and [exit N]
        case commandExit(ok: Bool, code: String)
        case other
    }
    let id = UUID()
    let raw: String
    let kind: Kind
}

/// Tails kernel.log by polling its size on a 0.5s timer and reading any new
/// bytes since the last offset. Mirrors `src/sbin/tui/state.py:316-390`'s
/// safety-net poll; we skip the FSEvents layer because polling alone is
/// good enough at this rate and log rotation handling is one fewer thing.
@MainActor
final class KernelLogTailer: ObservableObject {
    @Published private(set) var lines: [ActivityLine] = []

    private let path: URL = FHS.root
        .appendingPathComponent("var/log/kernel/kernel.log")
    private var offset: UInt64 = 0
    private var timer: Timer?
    private var inCommand = false
    private var outLines = 0
    private let maxBuffer = 1000

    init() {
        // Start at EOF so we don't replay the whole log on launch.
        if let attrs = try? FileManager.default.attributesOfItem(atPath: path.path),
           let size = attrs[.size] as? UInt64 {
            offset = size
        }
        timer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.poll() }
        }
    }

    deinit { timer?.invalidate() }

    private func poll() {
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: path.path),
              let size = attrs[.size] as? UInt64 else {
            return
        }
        if size < offset {
            // file was rotated/truncated — restart from the top.
            offset = 0
        }
        if size == offset { return }
        guard let handle = try? FileHandle(forReadingFrom: path) else { return }
        defer { try? handle.close() }
        do {
            try handle.seek(toOffset: offset)
        } catch { return }
        guard let chunk = try? handle.readToEnd(), !chunk.isEmpty else { return }
        offset = size
        guard let text = String(data: chunk, encoding: .utf8) else { return }
        var appended: [ActivityLine] = []
        for raw in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = String(raw)
            if line.isEmpty { continue }
            appended.append(classify(line))
        }
        if appended.isEmpty { return }
        lines.append(contentsOf: appended)
        if lines.count > maxBuffer {
            lines.removeFirst(lines.count - maxBuffer)
        }
    }

    private func classify(_ line: String) -> ActivityLine {
        if line.hasPrefix("[kernel] nudge:") {
            inCommand = false
            return ActivityLine(raw: line, kind: .nudgeStart)
        }
        if line.hasPrefix("[kernel] nudge failed") {
            inCommand = false
            return ActivityLine(raw: line, kind: .nudgeFail)
        }
        if line.hasPrefix("[kernel] nudge complete") {
            inCommand = false
            return ActivityLine(raw: line, kind: .nudgeDone)
        }
        if let m = paiPrefix(line) {
            let rest = String(line[m.upperBound...]).trimmingCharacters(in: .whitespaces)
            if rest.hasPrefix("$ ") {
                inCommand = true
                outLines = 0
                return ActivityLine(raw: line, kind: .paiCommand)
            }
            inCommand = false
            return ActivityLine(raw: line, kind: .paiSay)
        }
        if inCommand {
            let stripped = line.trimmingCharacters(in: .whitespaces)
            if stripped.hasPrefix("[exit") {
                inCommand = false
                let code = stripped.trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
                    .split(separator: " ").last.map(String.init) ?? "?"
                return ActivityLine(raw: line, kind: .commandExit(ok: code == "0", code: code))
            }
            if stripped == "[stderr]" {
                return ActivityLine(raw: line, kind: .other)
            }
            outLines += 1
            return ActivityLine(raw: line, kind: .commandOutput)
        }
        return ActivityLine(raw: line, kind: .other)
    }

    /// Matches `[pai:<digits>] ` or bare `[pai] ` and returns the trailing
    /// range so the caller can chop. Mirrors `_PAI_PREFIX` in widgets.py.
    private func paiPrefix(_ s: String) -> Range<String.Index>? {
        for prefix in ["[pai:", "[pai]"] {
            if s.hasPrefix(prefix) {
                if prefix == "[pai]" { return s.range(of: "[pai]") }
                guard let close = s.firstIndex(of: "]") else { return nil }
                return s.startIndex..<s.index(after: close)
            }
        }
        return nil
    }
}
