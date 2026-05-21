import Foundation

/// Launches the kernel and OWNS it: the kernel runs as a child of PAI.app and
/// is terminated when the app quits (see `terminateKernelSync()`, called from
/// `AppDelegate.applicationWillTerminate`). This is the consolidated model —
/// the app is the supervisor, not a detached background daemon.
///
/// Two interpreters, picked at runtime (see `pythonExecutable`):
///   - **Bundled** — `Contents/Resources/runtime/python/bin/python3`, a
///     self-contained interpreter + all deps + the kernel package, shipped
///     inside PAI.app by `macos/bundle-runtime.sh`. This is the shipped path
///     and the only one whose code-signing identity is PAI's.
///   - **Dev fallback** — `~/.pai/sbin/init` (the FHS venv shebang), used when
///     no bundled runtime is present, i.e. running from a plain Xcode build
///     against a `paifs-init`'d repo checkout.
@MainActor
final class KernelLauncher: ObservableObject {
    /// True while a Start click is mid-flight; the button uses this to
    /// debounce double-clicks (the kernel takes ~1s to register as online).
    @Published var inFlight: Bool = false
    /// Surfaced into the UI as an alert; mirrors `PAICloner.lastError`.
    @Published var lastError: String? = nil

    /// The kernel process while WE own it. nil once it exits or if it was
    /// started outside the app (CLI/dev) — in which case we fall back to the
    /// pid file for stop.
    private var kernel: Process?

    private var initURL: URL { FHS.root.appendingPathComponent("sbin/init") }
    private var kernelPidFile: URL {
        FHS.root.appendingPathComponent("run/kernel.pid")
    }

    /// Bundled self-contained interpreter, if this build shipped one.
    /// `Contents/Resources/runtime/python/bin/python3`.
    private var bundledPython: URL? {
        guard let res = Bundle.main.resourceURL else { return nil }
        let py = res.appendingPathComponent("runtime/python/bin/python3")
        return FileManager.default.isExecutableFile(atPath: py.path) ? py : nil
    }

    /// Launch the kernel as an owned child. Bundled mode runs the embedded
    /// python's `boot.init` (which verifies the FHS layout, then execs into
    /// `boot.entry` in-place — same pid, still our child). Dev mode runs
    /// `~/.pai/sbin/init`. `registry.kernelOnline` flips true on the next tick.
    func start() {
        guard !inFlight else { return }

        let exe: URL
        let args: [String]
        if let py = bundledPython {
            exe = py
            args = ["-u", "-m", "boot.init"]
        } else {
            exe = initURL
            args = []
            guard FileManager.default.isExecutableFile(atPath: exe.path) else {
                lastError = "no bundled runtime and \(exe.path) missing — run `paifs-init`?"
                return
            }
        }
        inFlight = true
        lastError = nil

        // Standalone mode: the on-disk tool shims (~/.pai/usr/bin, sbin) are
        // generated pointing at the dev venv; a shipped app must not depend on
        // it. Re-point them at our embedded interpreter before the kernel — and
        // the tools it spawns — run. Idempotent + non-fatal.
        if let py = bundledPython {
            repointShims(python: py)
        }

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
        proc.arguments = args
        proc.standardInput = FileHandle.nullDevice
        proc.standardOutput = logHandle
        proc.standardError = logHandle
        // The kernel reads/writes its FHS state here regardless of where the
        // code came from. Bundled mode also needs PAI_ROOT so boot.init finds
        // the on-disk layout; both modes inherit the rest of the environment.
        var env = ProcessInfo.processInfo.environment
        env["PAI_ROOT"] = FHS.root.path
        // The embedded interpreter has the kernel in its site-packages but NOT
        // the `drivers` namespace package — drivers are installed state under
        // <root>/usr/lib (see paifs-init's _pai_src.pth). Put it on the path so
        // `from drivers import …` resolves. Harmless in dev mode (the FHS venv
        // already has it via the .pth).
        let libPath = FHS.root.appendingPathComponent("usr/lib").path
        if let existing = env["PYTHONPATH"], !existing.isEmpty {
            env["PYTHONPATH"] = libPath + ":" + existing
        } else {
            env["PYTHONPATH"] = libPath
        }
        // A Finder-launched app inherits no shell PATH, so the kernel and the
        // subprocesses it spawns (services, hooks, CoreLocationCLI, paictl…)
        // wouldn't find the PAI bins. Prepend them, mirroring the kernel's own
        // `paths.prepend_pai_path()` and bash_tool/shell_tool. Belt and
        // suspenders: the kernel re-applies this, but the very first process
        // (and any pre-loop spawn) gets a sane PATH from us.
        let paiPath = [
            FHS.root.appendingPathComponent("usr/lib/venv/bin").path,
            FHS.root.appendingPathComponent("usr/bin").path,
            FHS.root.appendingPathComponent("sbin").path,
        ].joined(separator: ":")
        if let existing = env["PATH"], !existing.isEmpty {
            env["PATH"] = paiPath + ":" + existing
        } else {
            env["PATH"] = paiPath
        }
        proc.environment = env

        // We own the kernel: keep the Process so we can SIGTERM it on quit,
        // and clear our handle when it exits on its own.
        proc.terminationHandler = { [weak self] _ in
            Task { @MainActor in self?.kernel = nil }
        }
        do {
            try proc.run()
        } catch {
            try? logHandle.close()
            inFlight = false
            lastError = "failed to launch kernel: \(error.localizedDescription)"
            return
        }
        kernel = proc
        // Process retains the fd; close our copy so we don't keep an extra
        // reference around for the lifetime of the app.
        try? logHandle.close()

        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            self.inFlight = false
        }
    }

    /// Bind the on-disk tool shims to our embedded interpreter. They're
    /// generated at provision time against the dev venv (`paifs-init`); a
    /// standalone app must not depend on that venv, so we run the embedded
    /// python's `paifs_init --repoint-shims` to rewrite their shebangs at
    /// `python`. Fast, idempotent, and best-effort — a failure just leaves the
    /// shims as-is, so kernel start still proceeds.
    private func repointShims(python: URL) {
        let p = Process()
        p.executableURL = python
        p.arguments = ["-m", "bin.paifs_init", "--repoint-shims"]
        var env = ProcessInfo.processInfo.environment
        env["PAI_ROOT"] = FHS.root.path
        p.environment = env
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do {
            try p.run()
            p.waitUntilExit()
        } catch {
            // Best-effort: leave shims untouched and let the kernel start.
        }
    }

    /// Synchronous teardown for `applicationWillTerminate`: SIGTERM the kernel
    /// (ours by handle, else by pid file) and block briefly for a clean exit.
    /// The app must not return from `applicationWillTerminate` before the
    /// kernel has had a chance to shut its PAIs down.
    nonisolated func terminateKernelSync(timeout: TimeInterval = 3.0) {
        let pid: pid_t
        if let proc = MainActor.assumeIsolated({ self.kernel }), proc.isRunning {
            pid = proc.processIdentifier
        } else {
            let pidURL = FHS.root.appendingPathComponent("run/kernel.pid")
            guard let raw = try? String(contentsOf: pidURL, encoding: .utf8),
                  let p = pid_t(raw.trimmingCharacters(in: .whitespacesAndNewlines)),
                  p > 0, kill(p, 0) == 0
            else { return }
            pid = p
        }
        kill(pid, SIGTERM)
        // Poll for exit (kill(pid,0) == -1/ESRCH) up to the timeout.
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if kill(pid, 0) != 0 { return }
            usleep(50_000)
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
