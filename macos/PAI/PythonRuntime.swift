import Foundation

/// Shared access to the bundled interpreter for the app's "run a python child"
/// flows (capability catalog + installs). Mirrors the env Provisioner sets up:
/// PAI_ROOT, PYTHONPATH=usr/lib, and a real PATH (a Finder-launched app inherits
/// none) so paiman can shell out to `git`.
enum PythonRuntime {
    /// Bundled interpreter, if this build shipped one. Same probe as
    /// `Provisioner.bundledPython` / `KernelLauncher`.
    static var bundledPython: URL? {
        guard let res = Bundle.main.resourceURL else { return nil }
        let py = res.appendingPathComponent("runtime/python/bin/python3")
        return FileManager.default.isExecutableFile(atPath: py.path) ? py : nil
    }

    static func env() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        env["PAI_ROOT"] = FHS.root.path
        env["PYTHONPATH"] = FHS.root.appendingPathComponent("usr/lib").path
        let extra = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        if let existing = env["PATH"], !existing.isEmpty {
            env["PATH"] = extra + ":" + existing
        } else {
            env["PATH"] = extra
        }
        return env
    }

    /// Run the bundled python with `args`, streaming merged stdout+stderr through
    /// `onOutput`. Returns the exit status (-1 if launch failed). Off the main
    /// actor.
    nonisolated static func stream(
        _ args: [String], onOutput: @escaping @Sendable (String) -> Void
    ) async -> Int32 {
        guard let py = bundledPython else {
            onOutput("no bundled runtime\n")
            return -1
        }
        return await withCheckedContinuation { continuation in
            let proc = Process()
            proc.executableURL = py
            proc.arguments = args
            proc.environment = env()
            let pipe = Pipe()
            proc.standardOutput = pipe
            proc.standardError = pipe
            proc.standardInput = FileHandle.nullDevice
            let handle = pipe.fileHandleForReading
            handle.readabilityHandler = { h in
                let data = h.availableData
                if !data.isEmpty, let s = String(data: data, encoding: .utf8) {
                    onOutput(s)
                }
            }
            proc.terminationHandler = { p in
                handle.readabilityHandler = nil
                let rest = handle.readDataToEndOfFile()
                if !rest.isEmpty, let s = String(data: rest, encoding: .utf8) {
                    onOutput(s)
                }
                continuation.resume(returning: p.terminationStatus)
            }
            do {
                try proc.run()
            } catch {
                handle.readabilityHandler = nil
                onOutput("failed to launch: \(error.localizedDescription)\n")
                continuation.resume(returning: -1)
            }
        }
    }

    /// Run and collect all merged output. Uses `stream` under the hood so a
    /// chatty child (e.g. git clone progress) can't deadlock on a full pipe.
    nonisolated static func capture(_ args: [String]) async -> (status: Int32, output: String) {
        let box = OutputBox()
        let status = await stream(args) { box.append($0) }
        return (status, box.value)
    }
}

/// Thread-safe string accumulator: `stream`'s callback fires on a background
/// queue, so `capture` needs a lock around the buffer.
private final class OutputBox: @unchecked Sendable {
    private var s = ""
    private let lock = NSLock()
    func append(_ chunk: String) { lock.lock(); s += chunk; lock.unlock() }
    var value: String { lock.lock(); defer { lock.unlock() }; return s }
}
