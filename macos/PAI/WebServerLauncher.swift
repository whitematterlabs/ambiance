import Foundation

/// Spawns `pai_web` as a child of PAI.app, bound to a unix-domain socket at
/// `$PAI_ROOT/run/web.sock`. The Mac surface reaches it through `pai://` via
/// `PAIWebSchemeHandler`; no loopback TCP listener is opened, so the same
/// machine's port stays free for a future ngrok-tunneled remote surface.
///
/// Lifecycle mirrors `KernelLauncher`: launched at app startup, terminated on
/// `applicationWillTerminate`. Best-effort restart logic is intentionally
/// minimal — if the web server crashes, the menubar window will surface an
/// upstream error from the scheme handler and the user can relaunch the app.
@MainActor
final class WebServerLauncher: ObservableObject {
    @Published private(set) var running: Bool = false
    @Published private(set) var lastError: String? = nil

    private var process: Process?

    /// Where the web server listens. The .app's WKWebView talks to this path.
    var socketURL: URL {
        FHS.root.appendingPathComponent("run/web.sock")
    }

    /// Bundled python (preferred) or the dev venv's interpreter via the FHS
    /// `sbin/init` shebang. Same resolution as `KernelLauncher`.
    private var bundledPython: URL? {
        guard let res = Bundle.main.resourceURL else { return nil }
        let py = res.appendingPathComponent("runtime/python/bin/python3")
        return FileManager.default.isExecutableFile(atPath: py.path) ? py : nil
    }

    /// Dev fallback: the FHS venv's python (created by `paifs-init`). Not the
    /// `sbin/init` shebang — we need to import `usr.libexec.web.pai_web`, not
    /// run the kernel.
    private var devPython: URL? {
        let py = FHS.root.appendingPathComponent("usr/lib/venv/bin/python3")
        return FileManager.default.isExecutableFile(atPath: py.path) ? py : nil
    }

    func start() {
        guard process == nil else { return }
        let exe: URL
        if let py = bundledPython {
            exe = py
        } else if Bundle.main.bundleURL.pathExtension == "app" {
            // Same hard-fail contract as KernelLauncher: don't silently re-bind
            // the web sidecar's TCC identity to the dev venv interpreter.
            let msg = "PAI.app runtime missing — rebuild with: paibuild --full"
            FileHandle.standardError.write(Data((msg + "\n").utf8))
            lastError = msg
            exit(78)
        } else if let py = devPython {
            exe = py
        } else {
            lastError = "no python interpreter found for pai_web"
            return
        }

        let sock = socketURL
        do {
            try FileManager.default.createDirectory(
                at: sock.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
        } catch {
            lastError = "failed to create \(sock.deletingLastPathComponent().path): \(error.localizedDescription)"
            return
        }
        // Pre-clean any stale socket from a previous crashed run; pai_web also
        // does this defensively, but doing it here makes the bind path obvious.
        try? FileManager.default.removeItem(at: sock)

        let logURL = FHS.root.appendingPathComponent("var/log/kernel/web.log")
        do {
            try FileManager.default.createDirectory(
                at: logURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            if !FileManager.default.fileExists(atPath: logURL.path) {
                FileManager.default.createFile(atPath: logURL.path, contents: nil)
            }
        } catch {
            lastError = "failed to prepare web.log: \(error.localizedDescription)"
            return
        }
        guard let logHandle = try? FileHandle(forWritingTo: logURL) else {
            lastError = "failed to open web.log for writing"
            return
        }
        _ = try? logHandle.seekToEnd()

        let proc = Process()
        proc.executableURL = exe
        proc.arguments = [
            "-u", "-m", "usr.libexec.web.pai_web",
            "--unix-socket", sock.path,
        ]
        proc.standardInput = FileHandle.nullDevice
        proc.standardOutput = logHandle
        proc.standardError = logHandle

        var env = ProcessInfo.processInfo.environment
        env["PAI_ROOT"] = FHS.root.path
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        // pai_web is part of the kernel codebase but lives under
        // src/usr/libexec/web/. In bundled mode the wheel installed into the
        // embedded python's site-packages already has it; in dev mode we need
        // PYTHONPATH to include `src/` (it's there via the FHS .pth normally,
        // but be explicit so a sparse dev tree still works).
        let libPath = FHS.root.appendingPathComponent("usr/lib").path
        if let existing = env["PYTHONPATH"], !existing.isEmpty {
            env["PYTHONPATH"] = libPath + ":" + existing
        } else {
            env["PYTHONPATH"] = libPath
        }
        proc.environment = env

        proc.terminationHandler = { [weak self] _ in
            Task { @MainActor in
                self?.process = nil
                self?.running = false
            }
        }

        do {
            try proc.run()
        } catch {
            try? logHandle.close()
            lastError = "failed to launch pai_web: \(error.localizedDescription)"
            return
        }
        process = proc
        running = true
        try? logHandle.close()
    }

    /// Wait briefly for the socket file to appear so the first WKWebView load
    /// doesn't race the server. Polls every 50ms up to ~3s.
    func waitForReady(timeout: TimeInterval = 3.0) async {
        let sock = socketURL.path
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if FileManager.default.fileExists(atPath: sock) { return }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
    }

    nonisolated func terminateSync(timeout: TimeInterval = 2.0) {
        guard let proc = MainActor.assumeIsolated({ self.process }), proc.isRunning
        else { return }
        let pid = proc.processIdentifier
        kill(pid, SIGTERM)
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if kill(pid, 0) != 0 { return }
            usleep(50_000)
        }
        kill(pid, SIGKILL)
    }
}
