import Foundation

/// Shells out to `paiclone <source> -y`. Kept tiny — the actual clone
/// logic (config rewrite, instance dir, home stitch, reload event) lives
/// in `bin/paiclone.py` so TUI/CLI/this app share one implementation.
@MainActor
final class PAICloner: ObservableObject {
    /// Surfaced into the UI as a transient banner / alert.
    @Published var lastError: String? = nil
    /// Set while a clone is in flight so the sidebar can dim the "+" button.
    @Published var inFlight: Set<String> = []

    /// Resolve the `paiclone` shim. `paifs-init` generates this when the
    /// runtime is provisioned; if missing, we surface a useful error.
    private var paicloneURL: URL {
        FHS.root.appendingPathComponent("sbin/paiclone")
    }

    func clone(source: String) {
        guard !inFlight.contains(source) else { return }
        inFlight.insert(source)
        lastError = nil

        let exe = paicloneURL
        Task.detached(priority: .userInitiated) { [exe, source] in
            let result = Self.run(exe: exe, args: [source, "-y"])
            await MainActor.run {
                self.inFlight.remove(source)
                if let err = result {
                    self.lastError = err
                }
            }
        }
    }

    /// Returns nil on success, an error string on failure.
    nonisolated private static func run(exe: URL, args: [String]) -> String? {
        guard FileManager.default.isExecutableFile(atPath: exe.path) else {
            return "paiclone not found at \(exe.path) — run `paifs-init`?"
        }
        let proc = Process()
        proc.executableURL = exe
        proc.arguments = args
        let errPipe = Pipe()
        let outPipe = Pipe()
        proc.standardError = errPipe
        proc.standardOutput = outPipe
        do {
            try proc.run()
        } catch {
            return "failed to launch paiclone: \(error.localizedDescription)"
        }
        proc.waitUntilExit()
        if proc.terminationStatus == 0 { return nil }
        let stderr = String(
            data: errPipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8
        ) ?? ""
        let stdout = String(
            data: outPipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8
        ) ?? ""
        let body = stderr.isEmpty ? stdout : stderr
        return "paiclone exited \(proc.terminationStatus): \(body.trimmingCharacters(in: .whitespacesAndNewlines))"
    }
}
