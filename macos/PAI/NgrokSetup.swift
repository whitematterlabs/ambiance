import Foundation
import Combine

/// Optional first-run step (and toggle-on fallback) for the one-time ngrok
/// authtoken that remote access needs. The GUI twin of the user otherwise
/// having to run `ngrok config add-authtoken <tok>` in a terminal.
///
/// Remote access stays opt-in: this only *configures* ngrok so the later
/// "Enable remote access" toggle works without a terminal trip. It never turns
/// the tunnel on by itself.
@MainActor
final class NgrokSetup: ObservableObject {
    /// The token typed into the field. Handed straight to ngrok; never stored
    /// by us (ngrok writes it into its own config).
    @Published var token: String = ""
    @Published var busy: Bool = false
    @Published var error: String? = nil

    /// Set once the user has either configured or explicitly skipped, so the
    /// first-run step never re-nags. (Skipping is fine — they can still enable
    /// remote access later, which re-offers this same screen.)
    private let askedKey = "ngrok.setupAsked"

    /// Show the first-run step only on a shipped build that bundles ngrok, when
    /// no authtoken is configured yet and we haven't already asked. Mirrors how
    /// ModelSetup/CapabilityCatalog gate their first-run screens.
    var needsFirstRunSetup: Bool {
        guard Ngrok.isBundled, !Ngrok.isConfigured() else { return false }
        return !UserDefaults.standard.bool(forKey: askedKey)
    }

    var alreadyConfigured: Bool { Ngrok.isConfigured() }

    /// Mark the step as seen so it won't auto-appear at the next launch.
    func markAsked() { UserDefaults.standard.set(true, forKey: askedKey) }

    /// Persist the token via `ngrok config add-authtoken`. Returns true on
    /// success (also marks asked). Runs the blocking call off the main actor.
    func save() async -> Bool {
        let t = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return false }
        busy = true
        error = nil
        let err = await Task.detached { Ngrok.addAuthtoken(t) }.value
        busy = false
        if let err {
            error = err
            return false
        }
        token = ""
        markAsked()
        return true
    }
}
