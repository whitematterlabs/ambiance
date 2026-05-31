import Foundation

/// Shared ngrok helpers used by both the tunnel and the setup screens: locate
/// the binary (bundled or PATH), detect whether an authtoken is configured, and
/// store one via `ngrok config add-authtoken`.
///
/// We use ngrok's own global config (written by `add-authtoken`) rather than
/// managing the token ourselves — the bundled binary and a PATH ngrok both read
/// the same file, so configuring once works everywhere and survives reinstalls.
enum Ngrok {
    /// Bundled `runtime/bin/ngrok` (shipped app) or `ngrok` on PATH via env(1)
    /// (dev). Returns the executable plus any leading args (env needs "ngrok").
    static func resolveExecutable() -> (URL, [String])? {
        if let res = Bundle.main.resourceURL {
            let bundled = res.appendingPathComponent("runtime/bin/ngrok")
            if FileManager.default.isExecutableFile(atPath: bundled.path) {
                return (bundled, [])
            }
        }
        let env = URL(fileURLWithPath: "/usr/bin/env")
        if FileManager.default.isExecutableFile(atPath: env.path) {
            return (env, ["ngrok"])
        }
        return nil
    }

    /// True only when ngrok is bundled into the app (shipped build). Used to
    /// gate the first-run step the same way the model/runtime probes do — dev
    /// checkouts configure ngrok by hand and shouldn't see the wizard step.
    static var isBundled: Bool {
        guard let res = Bundle.main.resourceURL else { return false }
        let bundled = res.appendingPathComponent("runtime/bin/ngrok")
        return FileManager.default.isExecutableFile(atPath: bundled.path)
    }

    /// Whether an authtoken is already configured. Reads the env override and
    /// the known config locations for an `authtoken:` key (ngrok v3 default on
    /// macOS is Application Support; older paths kept for users upgrading).
    static func isConfigured() -> Bool {
        if let t = ProcessInfo.processInfo.environment["NGROK_AUTHTOKEN"], !t.isEmpty {
            return true
        }
        let home = FileManager.default.homeDirectoryForCurrentUser
        var candidates: [URL] = []
        if let cfg = ProcessInfo.processInfo.environment["NGROK_CONFIG"] {
            candidates.append(URL(fileURLWithPath: cfg))
        }
        candidates.append(contentsOf: [
            home.appendingPathComponent("Library/Application Support/ngrok/ngrok.yml"),
            home.appendingPathComponent(".config/ngrok/ngrok.yml"),
            home.appendingPathComponent(".ngrok2/ngrok.yml"),
        ])
        for url in candidates {
            if let text = try? String(contentsOf: url, encoding: .utf8),
               text.contains("authtoken:") {
                return true
            }
        }
        return false
    }

    /// Run `ngrok config add-authtoken <token>` synchronously and return `nil`
    /// on success or a short error string. Blocking — call off the main thread
    /// (e.g. from a detached Task).
    static func addAuthtoken(_ token: String) -> String? {
        guard let (exe, prefix) = resolveExecutable() else {
            return "ngrok isn't available in this build."
        }
        let proc = Process()
        proc.executableURL = exe
        proc.arguments = prefix + ["config", "add-authtoken", token]
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = pipe
        proc.environment = ProcessInfo.processInfo.environment
        do {
            try proc.run()
        } catch {
            return "Couldn't run ngrok: \(error.localizedDescription)"
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        if proc.terminationStatus != 0 {
            let out = String(data: data, encoding: .utf8) ?? ""
            let last = out.split(whereSeparator: \.isNewline).last.map(String.init)
            return "ngrok rejected the token\(last.map { ": \($0)" } ?? ".")"
        }
        return nil
    }

    /// Where to send the user to copy their token.
    static let dashboardURL = URL(string: "https://dashboard.ngrok.com/get-started/your-authtoken")!
}
