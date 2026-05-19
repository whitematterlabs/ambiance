import Foundation

/// Spawns `~/.pai/sbin/init` detached and wraps `pai-install-launchd` for the
/// "Start at login" toggle. Modeled on `PAICloner` — shells out, surfaces
/// errors via `lastError`, never holds onto the spawned process.
@MainActor
final class KernelLauncher: ObservableObject {
    /// True while a Start click is mid-flight; the button uses this to
    /// debounce double-clicks (the kernel takes ~1s to register as online).
    @Published var inFlight: Bool = false
    /// Mirrors `pai-install-launchd status` — 0 means the LaunchAgent plist
    /// is installed. Refreshed on init and after every toggle action.
    @Published var autostartEnabled: Bool = false
    /// Surfaced into the UI as an alert; mirrors `PAICloner.lastError`.
    @Published var lastError: String? = nil

    private var initURL: URL { FHS.root.appendingPathComponent("sbin/init") }
    private var installerURL: URL {
        FHS.root.appendingPathComponent("sbin/pai-install-launchd")
    }
    private var kernelPidFile: URL {
        FHS.root.appendingPathComponent("run/kernel.pid")
    }

    init() {
        refreshAutostart()
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

        let proc = Process()
        proc.executableURL = exe
        proc.standardInput = FileHandle.nullDevice
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice
        do {
            try proc.run()
        } catch {
            inFlight = false
            lastError = "failed to launch kernel: \(error.localizedDescription)"
            return
        }

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

    /// Run `pai-install-launchd status`; exit code 0 means installed.
    func refreshAutostart() {
        let exe = installerURL
        Task.detached(priority: .userInitiated) { [exe] in
            let result = Self.run(exe: exe, args: ["status"])
            await MainActor.run {
                self.autostartEnabled = (result.status == 0)
            }
        }
    }

    /// Run `pai-install-launchd install` or `uninstall`; surface stderr on
    /// failure and then re-check status so the toggle reflects truth.
    func setAutostart(_ enabled: Bool) {
        let exe = installerURL
        let verb = enabled ? "install" : "uninstall"
        guard FileManager.default.isExecutableFile(atPath: exe.path) else {
            lastError = "pai-install-launchd not found at \(exe.path) — run `paifs-init`?"
            refreshAutostart()
            return
        }
        Task.detached(priority: .userInitiated) { [exe, verb] in
            let result = Self.run(exe: exe, args: [verb])
            await MainActor.run {
                if result.status != 0 {
                    let body = result.stderr.trimmingCharacters(in: .whitespacesAndNewlines)
                    self.lastError = "pai-install-launchd \(verb) exited \(result.status): \(body)"
                }
                self.refreshAutostart()
            }
        }
    }

    /// Shape mirrors `PAICloner.run` — returns exit status + captured stderr.
    nonisolated private static func run(exe: URL, args: [String]) -> (status: Int32, stderr: String) {
        guard FileManager.default.isExecutableFile(atPath: exe.path) else {
            return (-1, "executable not found at \(exe.path)")
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
            return (-1, "failed to launch: \(error.localizedDescription)")
        }
        proc.waitUntilExit()
        let stderr = String(
            data: errPipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8
        ) ?? ""
        let stdout = String(
            data: outPipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8
        ) ?? ""
        return (proc.terminationStatus, stderr.isEmpty ? stdout : stderr)
    }
}
