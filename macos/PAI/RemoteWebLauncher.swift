import Foundation

/// The remote (ngrok-tunneled) twin of `WebServerLauncher`. Where the local
/// surface binds a unix socket (owner-only, no port), this one binds loopback
/// TCP on `port` and is gated by `--auth-token` — because the ngrok child then
/// puts that port on the public internet. Only this instance receives a token;
/// the local unix-socket surface and dev `pai start --web` stay unauthenticated.
///
/// Off by default: started only when the owner flips "Enable remote access",
/// torn down when they flip it off or quit (`terminateSync`).
@MainActor
final class RemoteWebLauncher: ObservableObject {
    @Published private(set) var running: Bool = false
    @Published private(set) var lastError: String? = nil

    /// Loopback port the remote pai_web listens on; ngrok forwards to it. The
    /// local surface uses a unix socket, so this port is always free.
    let port: Int = 8787
    var host: String { "127.0.0.1" }

    private var process: Process?

    /// Bundled python (preferred) or the dev venv interpreter. Same resolution
    /// as `WebServerLauncher` / `KernelLauncher`.
    private var bundledPython: URL? {
        guard let res = Bundle.main.resourceURL else { return nil }
        let py = res.appendingPathComponent("runtime/python/bin/python3")
        return FileManager.default.isExecutableFile(atPath: py.path) ? py : nil
    }

    private var devPython: URL? {
        let py = FHS.root.appendingPathComponent("usr/lib/venv/bin/python3")
        return FileManager.default.isExecutableFile(atPath: py.path) ? py : nil
    }

    /// Spawn `pai_web --port <port> --host 127.0.0.1 --auth-token <token>`.
    func start(authToken: String) {
        guard process == nil else { return }
        lastError = nil
        let exe: URL
        if let py = bundledPython {
            exe = py
        } else if Bundle.main.bundleURL.pathExtension == "app" {
            let msg = "PAI.app runtime missing — rebuild with: paibuild --full"
            FileHandle.standardError.write(Data((msg + "\n").utf8))
            lastError = msg
            return
        } else if let py = devPython {
            exe = py
        } else {
            lastError = "no python interpreter found for pai_web (remote)"
            return
        }

        let logURL = FHS.root.appendingPathComponent("var/log/kernel/web-remote.log")
        do {
            try FileManager.default.createDirectory(
                at: logURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            if !FileManager.default.fileExists(atPath: logURL.path) {
                FileManager.default.createFile(atPath: logURL.path, contents: nil)
            }
        } catch {
            lastError = "failed to prepare web-remote.log: \(error.localizedDescription)"
            return
        }
        guard let logHandle = try? FileHandle(forWritingTo: logURL) else {
            lastError = "failed to open web-remote.log for writing"
            return
        }
        _ = try? logHandle.seekToEnd()

        let proc = Process()
        proc.executableURL = exe
        proc.arguments = [
            "-u", "-m", "usr.libexec.web.pai_web",
            "--host", host,
            "--port", String(port),
            "--auth-token", authToken,
        ]
        proc.standardInput = FileHandle.nullDevice
        proc.standardOutput = logHandle
        proc.standardError = logHandle

        var env = ProcessInfo.processInfo.environment
        env["PAI_ROOT"] = FHS.root.path
        env["PYTHONDONTWRITEBYTECODE"] = "1"
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
            lastError = "failed to launch remote pai_web: \(error.localizedDescription)"
            return
        }
        process = proc
        running = true
        try? logHandle.close()
    }

    /// Poll `GET /api/health` over TCP until it answers (or timeout). Unlike the
    /// unix-socket launcher we can't just stat a socket file — we probe the HTTP
    /// endpoint, which is also what ngrok will forward to.
    @discardableResult
    func waitForReady(timeout: TimeInterval = 5.0) async -> Bool {
        guard let url = URL(string: "http://\(host):\(port)/api/health") else { return false }
        let deadline = Date().addingTimeInterval(timeout)
        var req = URLRequest(url: url)
        req.timeoutInterval = 1.0
        while Date() < deadline {
            if let (_, resp) = try? await URLSession.shared.data(for: req),
               let http = resp as? HTTPURLResponse, http.statusCode == 200 {
                return true
            }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        return false
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
