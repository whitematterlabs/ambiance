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
    let ctxTokens: Int        // last_window_tokens from /proc/<slug>/tokens, 0 if absent
    let treePrefix: String    // box-drawing indent for nested subagents
    let treeOrder: Int        // pre-order traversal index — default sort key
    var id: String { slug }
}

/// Compact token count: "—" if zero, else "12.3k" / "187k" / "1.2M".
/// Mirrors `_fmt_ctx` in src/sbin/tui/widgets.py.
func formatCtx(_ n: Int) -> String {
    if n <= 0 { return "—" }
    if n < 1000 { return String(n) }
    if n < 10_000 { return String(format: "%.1fk", Double(n) / 1000) }
    if n < 1_000_000 { return "\(n / 1000)k" }
    return String(format: "%.1fM", Double(n) / 1_000_000)
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
        var raw: [(slug: String, kind: String, pid: Int?, parent: Int?,
                   status: String, description: String, when: String,
                   busy: BusyState?, ctxTokens: Int)] = []
        for entry in entries {
            let slug = entry.lastPathComponent
            if slug.hasPrefix(".") { continue }
            let statusURL = entry.appendingPathComponent("status")
            let specURL = entry.appendingPathComponent("spec.yaml")
            let status = (try? String(contentsOf: statusURL, encoding: .utf8))?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            guard let specRaw = try? String(contentsOf: specURL, encoding: .utf8) else { continue }
            let spec = MiniYAML.parseTopLevel(specRaw)
            raw.append((
                slug: slug,
                kind: spec["kind"] ?? "",
                pid: spec["pid"].flatMap(Int.init),
                parent: spec["parent"].flatMap(Int.init),
                status: status,
                description: spec["description"] ?? "",
                when: spec["deadline"] ?? spec["schedule"] ?? "",
                busy: readBusy(at: entry.appendingPathComponent("busy")),
                ctxTokens: readCtxTokens(at: entry.appendingPathComponent("tokens"))
            ))
        }

        // Tree pre-order with box-drawing prefixes. Mirrors
        // src/boot/proctree.py:order_as_tree.
        let ordered = orderAsTree(raw)
        var found: [ProcRow] = []
        for (idx, item) in ordered.enumerated() {
            let r = item.row
            found.append(ProcRow(
                slug: r.slug, kind: r.kind, pid: r.pid, parent: r.parent,
                status: r.status, description: r.description, when: r.when,
                busy: r.busy, ctxTokens: r.ctxTokens,
                treePrefix: item.prefix, treeOrder: idx
            ))
        }
        if found != rows { rows = found }
    }
}

private typealias RawProc = (slug: String, kind: String, pid: Int?, parent: Int?,
                             status: String, description: String, when: String,
                             busy: BusyState?, ctxTokens: Int)

/// Returns records in tree pre-order with a box-drawing prefix per row.
/// Roots (records with no pid, or whose parent pid is unknown) come first
/// sorted by pid; each root's descendants follow sorted by pid. Mirrors
/// `order_as_tree` in src/boot/proctree.py.
private func orderAsTree(_ records: [RawProc]) -> [(row: RawProc, prefix: String)] {
    let byId: [Int: Int] = Dictionary(uniqueKeysWithValues:
        records.enumerated().compactMap { (i, r) in r.pid.map { ($0, i) } }
    )
    var children: [Int: [Int]] = [:]
    var rootIdxs: [Int] = []
    for (i, r) in records.enumerated() {
        if let p = r.parent, byId[p] != nil {
            children[p, default: []].append(i)
        } else {
            rootIdxs.append(i)
        }
    }
    let pidSort: (Int, Int) -> Bool = { a, b in
        (records[a].pid ?? Int.max) < (records[b].pid ?? Int.max)
    }
    for k in children.keys { children[k]?.sort(by: pidSort) }
    rootIdxs.sort(by: pidSort)

    var out: [(row: RawProc, prefix: String)] = []
    func walk(_ idx: Int, prefix: String, isLast: Bool, isRoot: Bool) {
        let connector: String
        let childPrefix: String
        if isRoot {
            connector = ""
            childPrefix = ""
        } else {
            connector = isLast ? "└─ " : "├─ "
            childPrefix = isLast ? "   " : "│  "
        }
        out.append((records[idx], prefix + connector))
        guard let pid = records[idx].pid, let kids = children[pid] else { return }
        for (i, kid) in kids.enumerated() {
            walk(kid, prefix: prefix + childPrefix, isLast: i == kids.count - 1, isRoot: false)
        }
    }
    for root in rootIdxs {
        walk(root, prefix: "", isLast: true, isRoot: true)
    }
    return out
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

/// Reads `last_window_tokens` from /proc/<slug>/tokens (JSON written by
/// boot/tokens.py). 0 if the file is missing, unparseable, or the PAI
/// hasn't made an LLM call yet.
func readCtxTokens(at url: URL) -> Int {
    guard let data = try? Data(contentsOf: url),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    else { return 0 }
    if let n = obj["last_window_tokens"] as? Int { return n }
    if let n = obj["last_window_tokens"] as? Double { return Int(n) }
    return 0
}
