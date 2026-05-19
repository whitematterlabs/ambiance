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
        // Authoritative: read run/kernel.pid (written by `boot.entry`) and
        // probe with kill(pid, 0). `/proc/<slug>/status` files survive
        // crashes and lie, so checking them as a fallback gives false
        // positives — don't.
        let pidFile = root.appendingPathComponent("run/kernel.pid")
        guard let raw = try? String(contentsOf: pidFile, encoding: .utf8),
              let pid = pid_t(raw.trimmingCharacters(in: .whitespacesAndNewlines)),
              pid > 0
        else { return false }
        // kill(pid, 0): 0 = alive, -1 + errno=ESRCH = dead, -1 + EPERM = alive
        // but owned by another uid (counts as alive).
        if kill(pid, 0) == 0 { return true }
        return errno == EPERM
    }
}
