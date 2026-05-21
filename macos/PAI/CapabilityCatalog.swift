import Foundation
import Combine

extension Notification.Name {
    /// Posted by the status-bar menu's "Add capabilities…" item; AppDelegate
    /// opens the capability window in response.
    static let openCapabilities = Notification.Name("pai.openCapabilities")
}

/// The owner-facing capability picker — GUI twin of `paisetup`'s curses
/// checklist. Loads the registry catalog via the embedded python
/// (`sbin.paisetup --json`), lets the owner pick drivers/skills/subagents, and
/// installs the selection via `paiman install`. PAI bundles are excluded: that's
/// paiadd's job, which is PAI's own tool, not owner-facing.
///
/// Runs once on first launch (gated by `var/lib/.setup-done`) and is reachable
/// any time from the status-bar menu, since `paiman install` works whenever.
@MainActor
final class CapabilityCatalog: ObservableObject {
    /// On-disk schema for the `.setup-done` marker. Swift-owned (the JSON
    /// catalog has its own independent `schema` field). Bump to re-prompt.
    static let schema = 1

    struct Item: Identifiable, Decodable, Equatable {
        let name: String
        let description: String
        let installed: Bool
        /// Registry-relative typed ref (e.g. "subagents/browse"). Passed to
        /// `paiman install` so a name shared across kinds resolves to the exact
        /// package the picker showed. Falls back to `name`.
        let ref: String
        var id: String { name }
    }
    struct Group: Identifiable {
        let kind: String
        let title: String
        var items: [Item]
        var id: String { kind }
    }

    @Published var groups: [Group] = []
    @Published var selected: Set<String> = []   // names checked for install
    @Published var loading = false
    @Published var installing = false
    @Published var loadError: String? = nil
    @Published var log: String = ""
    @Published var failures: [String] = []
    @Published var done = false                  // flips when the step concludes

    private var markerURL: URL { FHS.root.appendingPathComponent("var/lib/.setup-done") }

    /// True for a shipped build whose capability step hasn't run (or predates
    /// the current schema). Dev builds (no bundled runtime) never need it.
    var needsSetup: Bool {
        guard PythonRuntime.bundledPython != nil else { return false }
        guard let raw = try? String(contentsOf: markerURL, encoding: .utf8),
              let v = Int(raw.trimmingCharacters(in: .whitespacesAndNewlines))
        else { return true }
        return v < Self.schema
    }

    private static let titles = ["driver": "Drivers", "skill": "Skills", "subagent": "Subagents"]
    private static let order = ["driver", "skill", "subagent"]

    private struct Payload: Decodable {
        let schema: Int
        let auto_checked: [String]
        let groups: [String: [Item]]
    }

    /// Clear state so a menu-triggered reopen reloads fresh installed-state and
    /// re-applies the default selection. The view's `.task` reloads when
    /// `groups` is empty.
    func prepareForReopen() {
        groups = []
        selected = []
        log = ""
        failures = []
        loadError = nil
        done = false
    }

    func load() async {
        loading = true
        loadError = nil
        selected = []
        let (status, out) = await PythonRuntime.capture(["-u", "-m", "sbin.paisetup", "--json"])
        loading = false
        guard status == 0 else {
            loadError = "Couldn't load the capability catalog (exit \(status)). It needs git and a network connection."
            return
        }
        guard let data = Self.extractJSON(out),
              let payload = try? JSONDecoder().decode(Payload.self, from: data) else {
            loadError = "Couldn't read the capability catalog."
            return
        }
        groups = Self.order.map { kind in
            Group(kind: kind, title: Self.titles[kind] ?? kind, items: payload.groups[kind] ?? [])
        }
        // Default selection mirrors picker.py: auto-checked kinds, non-installed.
        let autoKinds = Set(payload.auto_checked)
        for g in groups where autoKinds.contains(g.kind) {
            for it in g.items where !it.installed { selected.insert(it.name) }
        }
    }

    /// The catalog prints one JSON object on its own stdout line; git progress
    /// is merged from stderr. Take the last line that parses as our object.
    private static func extractJSON(_ s: String) -> Data? {
        for line in s.split(separator: "\n").reversed() {
            let t = line.trimmingCharacters(in: .whitespaces)
            if t.hasPrefix("{"), let d = t.data(using: .utf8) { return d }
        }
        return nil
    }

    func toggle(_ name: String) {
        if selected.contains(name) { selected.remove(name) } else { selected.insert(name) }
    }

    var installCount: Int {
        groups.flatMap(\.items).filter { selected.contains($0.name) && !$0.installed }.count
    }

    func install() async {
        let picks = groups.flatMap(\.items)
            .filter { selected.contains($0.name) && !$0.installed }
        guard !picks.isEmpty else { skip(); return }
        installing = true
        failures = []
        log = ""
        for item in picks {
            log += "\n--- installing \(item.name) ---\n"
            // Install by typed ref so a cross-kind name collision (e.g.
            // bin/browse vs subagents/browse) resolves to the right package.
            let status = await PythonRuntime.stream(["-u", "-m", "bin.paiman", "install", item.ref]) {
                [weak self] chunk in Task { @MainActor in self?.log += chunk }
            }
            if status != 0 { failures.append(item.name) }
        }
        installing = false
        writeMarker()
        done = true
    }

    func skip() {
        writeMarker()
        done = true
    }

    private func writeMarker() {
        let url = markerURL
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? "\(Self.schema)\n".write(to: url, atomically: true, encoding: .utf8)
    }
}
