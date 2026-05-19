import Foundation
import Combine

/// One row from `~/.pai/proc/<slug>/`. Covers every process kind — pais,
/// drivers, crons, timers, services, deadlines — not just PAIs. Mirrors
/// `src/sbin/tui/state.py:201-231` ProcRow.
struct ProcRow: Identifiable, Hashable {
    let slug: String
    let kind: String          // pai / driver / cron / timer / service / deadline / ""
    let pid: Int?
    let parent: Int?
    let status: String        // running / completed / failed / ...
    let description: String
    let when: String          // schedule or deadline if any
    let busy: BusyState?
    var id: String { slug }
}

/// Present iff `~/.pai/proc/<slug>/busy` exists. Mirrors
/// `src/boot/processes.py:404-421`.
struct BusyState: Hashable {
    let reason: String
    let startedAt: Date?
    var elapsed: TimeInterval? {
        guard let s = startedAt else { return nil }
        return Date().timeIntervalSince(s)
    }
}

/// Polls `~/.pai/proc/` every second. Same trade-off as PAIRegistry: a few
/// stats + small reads, no need for FSEvents at this rate.
@MainActor
final class ProcRegistry: ObservableObject {
    @Published private(set) var rows: [ProcRow] = []

    private var timer: Timer?

    init() {
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
    }

    deinit { timer?.invalidate() }

    private func refresh() {
        guard let entries = try? FileManager.default.contentsOfDirectory(
            at: FHS.proc, includingPropertiesForKeys: nil
        ) else {
            if !rows.isEmpty { rows = [] }
            return
        }
        var found: [ProcRow] = []
        for entry in entries {
            let slug = entry.lastPathComponent
            if slug.hasPrefix(".") { continue }
            let statusURL = entry.appendingPathComponent("status")
            let specURL = entry.appendingPathComponent("spec.yaml")
            let status = (try? String(contentsOf: statusURL, encoding: .utf8))?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            guard let specRaw = try? String(contentsOf: specURL, encoding: .utf8) else { continue }
            let spec = MiniYAML.parseTopLevel(specRaw)
            let kind = spec["kind"] ?? ""
            let pid = spec["pid"].flatMap(Int.init)
            let parent = spec["parent"].flatMap(Int.init)
            let desc = spec["description"] ?? ""
            let when = spec["deadline"] ?? spec["schedule"] ?? ""
            let busy = readBusy(at: entry.appendingPathComponent("busy"))
            found.append(ProcRow(
                slug: slug, kind: kind, pid: pid, parent: parent,
                status: status, description: desc, when: when, busy: busy
            ))
        }
        // PAIs first (by pid), then everything else alphabetically.
        found.sort {
            if ($0.kind == "pai") != ($1.kind == "pai") { return $0.kind == "pai" }
            if $0.kind == "pai" && $1.kind == "pai" {
                return ($0.pid ?? .max) < ($1.pid ?? .max)
            }
            return $0.slug < $1.slug
        }
        if found != rows { rows = found }
    }
}

/// Reads the two-line busy file. First line = reason, second line =
/// Unix timestamp (float). Either may be missing.
func readBusy(at url: URL) -> BusyState? {
    guard let raw = try? String(contentsOf: url, encoding: .utf8) else { return nil }
    let lines = raw.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
    let reason = lines.first?.trimmingCharacters(in: .whitespaces) ?? ""
    var started: Date? = nil
    if lines.count >= 2, let ts = Double(lines[1].trimmingCharacters(in: .whitespaces)) {
        started = Date(timeIntervalSince1970: ts)
    }
    return BusyState(reason: reason, startedAt: started)
}
