import Foundation
import SwiftUI

/// Orchestrates the opt-in remote-access feature: the second `pai_web` (TCP,
/// loopback, token-gated) and the ngrok tunnel in front of it. Owned by
/// `AppDelegate`; bound into the StatusBar toggle and the QR panel.
///
/// Off by default. Toggle-on mints a fresh access code (so it's "short-lived" —
/// a new code per session), starts the remote web child, waits for it to answer
/// `/api/health`, then starts ngrok and reads its public URL. Toggle-off (or
/// app quit) tears ngrok down first, then the web child, so the public URL
/// goes dark before the thing it points at.
@MainActor
final class RemoteAccess: ObservableObject {
    enum Phase: Equatable {
        case off
        case needsAuth   // toggled on, but ngrok has no authtoken yet
        case starting
        case ready
        case failed(String)
    }

    @Published private(set) var phase: Phase = .off
    @Published private(set) var publicURL: String? = nil
    @Published private(set) var accessCode: String? = nil

    private let web = RemoteWebLauncher()
    private let tunnel = TunnelLauncher()

    var isOn: Bool {
        switch phase {
        case .off: return false
        default: return true
        }
    }

    /// The string the QR encodes and the phone opens: the public URL with the
    /// access code as a query param. `auth.ts` lifts the token out and strips it.
    var connectURL: String? {
        guard let url = publicURL, let code = accessCode else { return nil }
        let base = url.hasSuffix("/") ? String(url.dropLast()) : url
        return "\(base)/?token=\(code)"
    }

    func toggle(_ on: Bool) {
        if on { enable() } else { disable() }
    }

    private func enable() {
        // Already on or coming up — nothing to do. (Re-entrant from .needsAuth
        // once the user supplies a token, and from .failed on retry.)
        switch phase {
        case .starting, .ready: return
        default: break
        }
        // Pre-flight: ngrok needs a one-time authtoken. Surface the in-app setup
        // (first-run, or the panel's token field) instead of failing into the
        // tunnel and reporting it after the fact.
        guard Ngrok.isConfigured() else {
            phase = .needsAuth
            return
        }
        let code = Self.mintCode()
        accessCode = code
        publicURL = nil
        phase = .starting

        Task { @MainActor in
            web.start(authToken: code)
            if let err = web.lastError {
                return fail("Couldn't start the remote server: \(err)")
            }
            let up = await web.waitForReady()
            guard self.stillStarting() else { return }
            guard up else {
                return fail("Remote server didn't come up in time.")
            }
            tunnel.start(port: web.port)
            if let err = tunnel.lastError {
                return fail(err)
            }
            let url = await tunnel.fetchPublicURL()
            guard self.stillStarting() else { return }
            guard let url else {
                return fail(tunnel.lastError ?? "Couldn't open the tunnel.")
            }
            publicURL = url
            phase = .ready
        }
    }

    /// True while we're still mid-startup; if the user flipped remote access
    /// back off during an `await`, tear down what we started and bail.
    private func stillStarting() -> Bool {
        if case .starting = phase { return true }
        tunnel.terminateSync()
        web.terminateSync()
        return false
    }

    /// Stop ngrok first (kills the public URL), then the web child.
    private func disable() {
        tunnel.terminateSync()
        web.terminateSync()
        publicURL = nil
        accessCode = nil
        phase = .off
    }

    private func fail(_ message: String) {
        tunnel.terminateSync()
        web.terminateSync()
        publicURL = nil
        phase = .failed(message)
    }

    /// Synchronous teardown for `applicationWillTerminate`.
    nonisolated func terminateSync() {
        tunnel.terminateSync()
        web.terminateSync()
    }

    /// A short, unambiguous access code: 8 chars of a reduced base32 alphabet
    /// (no 0/1/l/o lookalikes — it gets typed by hand on the "password" path).
    /// `Int.random` draws from the system CSPRNG on Apple platforms.
    private static func mintCode() -> String {
        let alphabet = Array("abcdefghjkmnpqrstuvwxyz23456789")
        return String((0..<8).map { _ in alphabet[Int.random(in: 0..<alphabet.count)] })
    }
}
