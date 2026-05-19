import Foundation
import Combine

/// One running PAI as seen on disk.
struct PAIInfo: Identifiable, Hashable {
    let slug: String
    let pid: Int
    let description: String
    let busy: BusyState?
    var id: Int { pid }
}

/// Polls `~/.pai/proc/<slug>/spec.yaml` + `status` to enumerate kind:pai
/// processes whose status == "running". Mirrors `src/sbin/tui/app.py:211-225`.
///
/// Uses a 1s timer instead of FSEvents — proc/ updates are infrequent and
/// the cost is one readdir + a few small reads. Keeps the dependency
/// surface tiny (no watchdog-equivalent for Swift).
@MainActor
final class PAIRegistry: ObservableObject {
    @Published private(set) var pais: [PAIInfo] = []
    @Published private(set) var kernelOnline: Bool = false

    private var timer: Timer?

    // Started from init — NOT from PAIApp, because accessing @StateObject
    // in App.init creates a throwaway instance, and the real one (the one
    // views observe) would never have its timer started.
    init() {
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
    }

    deinit {
        timer?.invalidate()
    }

    private func refresh() {
        let online = FHS.kernelIsRunning
        if online != kernelOnline { kernelOnline = online }

        guard let entries = try? FileManager.default.contentsOfDirectory(
            at: FHS.proc, includingPropertiesForKeys: nil
        ) else {
            if !pais.isEmpty { pais = [] }
            return
        }

        var found: [PAIInfo] = []
        for entry in entries {
            let slug = entry.lastPathComponent
            if slug.hasPrefix(".") { continue }
            let statusURL = entry.appendingPathComponent("status")
            let specURL = entry.appendingPathComponent("spec.yaml")
            guard
                let statusRaw = try? String(contentsOf: statusURL, encoding: .utf8),
                statusRaw.trimmingCharacters(in: .whitespacesAndNewlines) == "running",
                let specRaw = try? String(contentsOf: specURL, encoding: .utf8)
            else { continue }

            let spec = MiniYAML.parseTopLevel(specRaw)
            guard spec["kind"] == "pai" else { continue }
            guard let pidStr = spec["pid"], let pid = Int(pidStr) else { continue }
            let desc = spec["description"] ?? slug
            let busy = readBusy(at: entry.appendingPathComponent("busy"))
            found.append(PAIInfo(slug: slug, pid: pid, description: desc, busy: busy))
        }
        found.sort { $0.pid < $1.pid }
        if found != pais { pais = found }
    }
}

/// Tiny single-purpose YAML reader: extracts top-level scalar key/value
/// pairs from a flat mapping (good enough for spec.yaml). Anything nested
/// is ignored. Avoids pulling in a Swift YAML dependency for the MVP.
enum MiniYAML {
    static func parseTopLevel(_ text: String) -> [String: String] {
        var out: [String: String] = [:]
        for raw in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = String(raw)
            if line.isEmpty || line.first == "#" { continue }
            if line.first == " " || line.first == "\t" { continue }
            guard let colon = line.firstIndex(of: ":") else { continue }
            let key = String(line[..<colon]).trimmingCharacters(in: .whitespaces)
            var value = String(line[line.index(after: colon)...])
                .trimmingCharacters(in: .whitespaces)
            // strip surrounding quotes
            if value.count >= 2 {
                let first = value.first!, last = value.last!
                if (first == "\"" && last == "\"") || (first == "'" && last == "'") {
                    value = String(value.dropFirst().dropLast())
                }
            }
            if value.isEmpty { continue }
            out[key] = value
        }
        return out
    }
}
