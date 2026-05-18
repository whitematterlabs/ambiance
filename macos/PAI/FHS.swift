import Foundation

/// Resolves the PAI FHS root. Mirrors `src/boot/paths.py:19-23`:
/// `$PAI_ROOT` env var if set, otherwise `~/.pai`.
enum FHS {
    static let root: URL = {
        if let env = ProcessInfo.processInfo.environment["PAI_ROOT"], !env.isEmpty {
            return URL(fileURLWithPath: (env as NSString).expandingTildeInPath, isDirectory: true)
        }
        let home = FileManager.default.homeDirectoryForCurrentUser
        return home.appendingPathComponent(".pai", isDirectory: true)
    }()

    static var proc: URL { root.appendingPathComponent("proc", isDirectory: true) }
    static var eventsDir: URL { root.appendingPathComponent("run/pai/events", isDirectory: true) }
    static var meRoot: URL {
        root.appendingPathComponent("home/pai/communication/messages/me", isDirectory: true)
    }

    /// Canonical day-file path for a PAI pid.
    /// Mirrors `src/sbin/tui/state.py:40-41` and `src/boot/nudge.py:97-98`.
    static func dayFile(pid: Int, date: Date = Date()) -> URL {
        let df = DateFormatter()
        df.calendar = Calendar(identifier: .gregorian)
        df.locale = Locale(identifier: "en_US_POSIX")
        df.timeZone = TimeZone.current
        df.dateFormat = "yyyy-MM-dd"
        let stamp = df.string(from: date)
        return meRoot
            .appendingPathComponent(String(pid), isDirectory: true)
            .appendingPathComponent("\(stamp).md")
    }

    static var kernelIsRunning: Bool {
        // Heuristic: at least one /proc/<slug>/ exists with status == "running".
        guard let entries = try? FileManager.default.contentsOfDirectory(
            at: proc, includingPropertiesForKeys: nil
        ) else { return false }
        for entry in entries {
            let status = entry.appendingPathComponent("status")
            if let s = try? String(contentsOf: status, encoding: .utf8),
               s.trimmingCharacters(in: .whitespacesAndNewlines) == "running" {
                return true
            }
        }
        return false
    }
}
