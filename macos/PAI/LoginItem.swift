import Foundation
import ServiceManagement

/// Launch-at-login toggle. Registers PAI.app *itself* (which owns the kernel)
/// as a login item via SMAppService — deliberately not a headless launchd
/// kernel: the app is the supervisor, so autostarting the app starts the
/// kernel as its child.
///
/// SMAppService's registration is the system source of truth and persists
/// across launches; we mirror it into UserDefaults so the toggle reflects
/// intent immediately and survives a status query that briefly lags.
@MainActor
final class LoginItem: ObservableObject {
    private static let defaultsKey = "launchAtLogin"

    /// Reflects the live SMAppService registration. Bind a Toggle to it.
    @Published private(set) var isEnabled: Bool
    /// Surfaced as an alert/help when register/unregister throws.
    @Published var lastError: String?

    init() {
        isEnabled = SMAppService.mainApp.status == .enabled
        // Reconcile the persisted hint on first read (status can be .notFound
        // before the first register on some macOS versions).
        if !isEnabled, UserDefaults.standard.bool(forKey: Self.defaultsKey) {
            isEnabled = SMAppService.mainApp.status == .enabled
        }
    }

    /// Re-sync from the system, e.g. when the window reappears and the user may
    /// have toggled the item in System Settings → Login Items.
    func refresh() {
        isEnabled = SMAppService.mainApp.status == .enabled
    }

    func setEnabled(_ on: Bool) {
        do {
            if on {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
            UserDefaults.standard.set(on, forKey: Self.defaultsKey)
            isEnabled = on
            lastError = nil
        } catch {
            lastError = "Couldn't \(on ? "enable" : "disable") launch at login: \(error.localizedDescription)"
            isEnabled = SMAppService.mainApp.status == .enabled
        }
    }

    func toggle() { setEnabled(!isEnabled) }
}
