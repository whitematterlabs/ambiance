import Foundation

/// Spawns `~/.pai/sbin/init` detached so the kernel outlives the app.
/// Modeled on `PAICloner` — shells out, surfaces errors via `lastError`,
/// never holds onto the spawned process.
@MainActor
final class KernelLauncher: ObservableObject {
    /// True while a Start click is mid-flight; the button uses this to
    /// debounce double-clicks (the kernel takes ~1s to register as online).
    @Published var inFlight: Bool = false
    /// Surfaced into the UI as an alert; mirrors `PAICloner.lastError`.
    @Published var lastError: String? = nil

    private var initURL: URL { FHS.root.appendingPathComponent("sbin/init") }
    private var kernelPidFile: URL {
        FHS.root.appendingPathComponent("run/kernel.pid")
    }

    /// One-shot: spawn `sbin/init` with stdio detached. We do NOT wait —
    /// the kernel needs to outlive the app. `registry.kernelOnline` will
    /// flip true on the next poll tick.
    func start() {
        guard !inFlight else { return }
        let exe = initURL
        guard FileManager.default.isExecutableFile(atPath: exe.path) else {
            lastError = "init not found at \(exe.path) — run `paifs-init`?"
            return
        }
        inFlight = true
        lastError = nil

        let logURL = FHS.root.appendingPathComponent("var/log/kernel/kernel.log")
        do {
            try FileManager.default.createDirectory(
                at: logURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            if !FileManager.default.fileExists(atPath: logURL.path) {
                FileManager.default.createFile(atPath: logURL.path, contents: nil)
            }
        } catch {
            inFlight = false
            lastError = "failed to prepare kernel.log: \(error.localizedDescription)"
            return
        }
        // Append-only handle so the kernel's stdout/stderr land in kernel.log
        // instead of /dev/null. _install_stdout_tee in src/boot/main.py skips
        // the tee when stdout isn't a TTY ("caller owns the log"), so this
        // app IS that caller.
        guard let logHandle = try? FileHandle(forWritingTo: logURL) else {
            inFlight = false
            lastError = "failed to open kernel.log for writing"
            return
        }
        _ = try? logHandle.seekToEnd()

        let proc = Process()
        proc.executableURL = exe
        proc.standardInput = FileHandle.nullDevice
        proc.standardOutput = logHandle
        proc.standardError = logHandle
        do {
            try proc.run()
        } catch {
            try? logHandle.close()
            inFlight = false
            lastError = "failed to launch kernel: \(error.localizedDescription)"
            return
        }
        // Process retains the fd; close our copy so we don't keep an extra
        // reference around for the lifetime of the app.
        try? logHandle.close()

        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            self.inFlight = false
        }
    }

    /// Send SIGTERM to the kernel pid recorded at `run/kernel.pid`. The
    /// kernel's SIGTERM handler (`src/boot/main.py:918`) shuts down its PAIs
    /// and exits cleanly. `registry.kernelOnline` will flip false on the
    /// next poll tick.
    func stop() {
        guard !inFlight else { return }
        guard let raw = try? String(contentsOf: kernelPidFile, encoding: .utf8),
              let pid = pid_t(raw.trimmingCharacters(in: .whitespacesAndNewlines)),
              pid > 0
        else {
            lastError = "no kernel pid at \(kernelPidFile.path)"
            return
        }
        inFlight = true
        lastError = nil
        if kill(pid, SIGTERM) != 0 {
            let err = String(cString: strerror(errno))
            lastError = "kill(\(pid), SIGTERM) failed: \(err)"
        }
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            self.inFlight = false
        }
    }
}
