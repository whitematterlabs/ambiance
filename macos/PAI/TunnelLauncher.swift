import Foundation

/// Owns the `ngrok http <port>` child that exposes the loopback remote
/// `pai_web` to the public internet. ngrok runs a local API at
/// `127.0.0.1:4040`; after start we poll `/api/tunnels` for the assigned
/// `public_url`. Like the launchers it owns its process and `terminateSync`s
/// it on toggle-off / quit.
///
/// ngrok needs a one-time `ngrok config add-authtoken <tok>` by the owner
/// (its authtoken lives in ~/.config/ngrok and is read by the bundled binary
/// too). If it isn't configured, ngrok exits early with no tunnel — we surface
/// that as a clear, actionable error rather than a silent dead URL.
@MainActor
final class TunnelLauncher: ObservableObject {
    @Published private(set) var running: Bool = false
    @Published private(set) var publicURL: String? = nil
    @Published private(set) var lastError: String? = nil

    private var process: Process?
    private let apiURL = URL(string: "http://127.0.0.1:4040/api/tunnels")!

    private var logURL: URL {
        FHS.root.appendingPathComponent("var/log/kernel/ngrok.log")
    }

    /// Bundled ngrok (`Contents/Resources/runtime/bin/ngrok`) if present;
    /// otherwise resolve `ngrok` on the inherited PATH (dev fallback).
    private func resolveExecutable() -> (URL, [String])? {
        if let res = Bundle.main.resourceURL {
            let bundled = res.appendingPathComponent("runtime/bin/ngrok")
            if FileManager.default.isExecutableFile(atPath: bundled.path) {
                return (bundled, [])
            }
        }
        // Dev: defer to PATH resolution via env(1).
        let env = URL(fileURLWithPath: "/usr/bin/env")
        if FileManager.default.isExecutableFile(atPath: env.path) {
            return (env, ["ngrok"])
        }
        return nil
    }

    /// Spawn `ngrok http <port>` (loopback target). Returns immediately; the
    /// public URL is read asynchronously via `fetchPublicURL()`.
    func start(port: Int) {
        guard process == nil else { return }
        lastError = nil
        publicURL = nil

        guard let (exe, prefixArgs) = resolveExecutable() else {
            lastError = "ngrok not found — bundle it (paibuild) or install it on PATH"
            return
        }

        do {
            try FileManager.default.createDirectory(
                at: logURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
        } catch {
            lastError = "failed to prepare ngrok.log: \(error.localizedDescription)"
            return
        }
        // Fresh log each session so error scanning sees only this run.
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        guard let logHandle = try? FileHandle(forWritingTo: logURL) else {
            lastError = "failed to open ngrok.log for writing"
            return
        }

        let proc = Process()
        proc.executableURL = exe
        proc.arguments = prefixArgs + ["http", String(port), "--log", "stdout"]
        proc.standardInput = FileHandle.nullDevice
        proc.standardOutput = logHandle
        proc.standardError = logHandle
        proc.environment = ProcessInfo.processInfo.environment

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
            lastError = "failed to launch ngrok: \(error.localizedDescription)"
            return
        }
        process = proc
        running = true
        try? logHandle.close()
    }

    /// Poll the ngrok local API for the assigned public URL. Prefers the HTTPS
    /// tunnel. On timeout, infer the likely cause from the log (missing
    /// authtoken is by far the common one) and set `lastError`.
    @discardableResult
    func fetchPublicURL(timeout: TimeInterval = 10.0) async -> String? {
        let deadline = Date().addingTimeInterval(timeout)
        var req = URLRequest(url: apiURL)
        req.timeoutInterval = 1.0
        while Date() < deadline {
            // If ngrok already exited, stop waiting and report.
            if process == nil {
                lastError = tunnelFailureReason()
                return nil
            }
            if let (data, resp) = try? await URLSession.shared.data(for: req),
               let http = resp as? HTTPURLResponse, http.statusCode == 200,
               let url = parsePublicURL(from: data) {
                publicURL = url
                return url
            }
            try? await Task.sleep(nanoseconds: 250_000_000)
        }
        lastError = tunnelFailureReason()
        return nil
    }

    /// Pull `tunnels[].public_url` from the ngrok API payload, preferring https.
    private func parsePublicURL(from data: Data) -> String? {
        guard
            let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let tunnels = obj["tunnels"] as? [[String: Any]], !tunnels.isEmpty
        else { return nil }
        let urls = tunnels.compactMap { $0["public_url"] as? String }
        return urls.first(where: { $0.hasPrefix("https://") }) ?? urls.first
    }

    /// Best-effort cause from the ngrok log tail; defaults to the authtoken hint.
    private func tunnelFailureReason() -> String {
        let log = (try? String(contentsOf: logURL, encoding: .utf8)) ?? ""
        if log.contains("authtoken") || log.contains("ERR_NGROK_4018")
            || log.contains("account") {
            return "ngrok needs a one-time setup. Run in Terminal:\n"
                + "    ngrok config add-authtoken <your token>\n"
                + "Get a free token at https://dashboard.ngrok.com"
        }
        if log.isEmpty {
            return "ngrok did not start — is it installed and on PATH?"
        }
        // Surface the last non-empty line for anything unexpected.
        let lastLine = log.split(whereSeparator: \.isNewline).last.map(String.init) ?? ""
        return "ngrok failed to open a tunnel. \(lastLine)"
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
