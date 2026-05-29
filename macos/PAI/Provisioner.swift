import Foundation
import Combine

/// First-run provisioning of `~/.pai` from a shipped PAI.app. Mirrors
/// KernelLauncher's "the app owns a python child" model: it runs the embedded
/// interpreter's `bin.paifs_init --bundle-mode --seed <Resources/seed>` to lay
/// out the FHS, copy the bundled seed content, generate tool shims at the
/// embedded python, and seed kernel-essential packages from the bundled
/// pairegistry copy at `Resources/seed/registry/` (PAIMAN_REGISTRY points at it).
///
/// No `git` or network needed on first run — the registry ships with the app.
/// A failure surfaces as `lastError` in the setup window, with a Retry.
///
/// Dev builds (no bundled runtime) never provision here — a repo checkout +
/// `paifs-init` already laid out `~/.pai`.
@MainActor
final class Provisioner: ObservableObject {
    /// On-disk provisioning schema this build produces. MUST match
    /// `PROVISION_SCHEMA` in src/bin/paifs_init.py — bump both together.
    static let schema = 1

    /// True while `provision()` runs; drives the setup window's spinner.
    @Published var inProgress = false
    /// Non-nil after a failed run; the setup window shows it + a Retry button.
    @Published var lastError: String? = nil
    /// Streamed stdout+stderr from paifs_init, tailed in the setup window.
    @Published var log: String = ""

    /// Bundled interpreter, if this build shipped one. Same probe as
    /// `KernelLauncher.bundledPython`.
    private var bundledPython: URL? {
        guard let res = Bundle.main.resourceURL else { return nil }
        let py = res.appendingPathComponent("runtime/python/bin/python3")
        return FileManager.default.isExecutableFile(atPath: py.path) ? py : nil
    }

    /// The staged seed dir (`Contents/Resources/seed`) shipped by
    /// macos/bundle-runtime.sh: holds `etc/` and `doc/`.
    private var seedDir: URL? {
        Bundle.main.resourceURL?.appendingPathComponent("seed")
    }

    private var markerURL: URL {
        FHS.root.appendingPathComponent("var/lib/.provisioned")
    }

    /// True when this is a shipped build whose `~/.pai` is absent or stamped at
    /// an older schema. Dev builds (no bundled runtime) never need it.
    var needsProvisioning: Bool {
        guard bundledPython != nil else { return false }
        guard let raw = try? String(contentsOf: markerURL, encoding: .utf8),
              let v = Int(raw.trimmingCharacters(in: .whitespacesAndNewlines))
        else { return true }  // no marker (or unreadable) ⇒ never provisioned
        return v < Self.schema
    }

    /// Run `paifs_init --bundle-mode`, streaming output into `log`. Sets
    /// `lastError` on failure (leaving the setup window up for a Retry).
    func provision() async {
        guard let py = bundledPython, let seed = seedDir else {
            lastError = "no bundled runtime — cannot provision"
            return
        }
        inProgress = true
        lastError = nil
        log = ""

        let result = await Self.run(python: py, seed: seed) { [weak self] chunk in
            Task { @MainActor in self?.log += chunk }
        }
        inProgress = false
        if let err = result { lastError = err }
    }

    /// Returns nil on success, an error string on failure. Off the main actor;
    /// pipes merged stdout+stderr through `onOutput`.
    nonisolated private static func run(
        python: URL, seed: URL, onOutput: @escaping @Sendable (String) -> Void
    ) async -> String? {
        await withCheckedContinuation { continuation in
            let proc = Process()
            proc.executableURL = python
            proc.arguments = [
                "-u", "-m", "bin.paifs_init", "--bundle-mode", "--seed", seed.path,
            ]
            var env = ProcessInfo.processInfo.environment
            env["PAI_ROOT"] = FHS.root.path
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            // The embedded interpreter has the package; the on-disk `drivers`
            // namespace resolves via PYTHONPATH=usr/lib (mirrors KernelLauncher).
            // On a clean root usr/lib is empty until paiman seeds it — harmless.
            env["PYTHONPATH"] = FHS.root.appendingPathComponent("usr/lib").path
            // Point paiman at the bundled pairegistry copy. paiman.py treats a
            // local-path value as `Path(loc).expanduser()` and skips the clone,
            // so no git + no network is required on first run.
            env["PAIMAN_REGISTRY"] = seed.appendingPathComponent("registry").path
            // A Finder-launched app inherits no shell PATH; add the usual bins
            // so any subprocesses paiman/paifs_init shell out to resolve.
            let extra = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
            if let existing = env["PATH"], !existing.isEmpty {
                env["PATH"] = extra + ":" + existing
            } else {
                env["PATH"] = extra
            }
            proc.environment = env

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
                if p.terminationStatus == 0 {
                    continuation.resume(returning: nil)
                } else {
                    continuation.resume(
                        returning: "provisioning failed (exit \(p.terminationStatus)). See log below."
                    )
                }
            }
            do {
                try proc.run()
            } catch {
                handle.readabilityHandler = nil
                continuation.resume(
                    returning: "failed to launch provisioner: \(error.localizedDescription)"
                )
            }
        }
    }
}
