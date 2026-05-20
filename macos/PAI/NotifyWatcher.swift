import Foundation
import UserNotifications

/// Tails `~/.pai/var/log/turns.jsonl` (written by `src/boot/nudge.py` after
/// every PAI turn) and posts a `UNUserNotificationCenter` notification per
/// new line. Title is `"<slug> is done!"`, body is empty.
///
/// Backlog suppression: on launch we seek to EOF, so missed turns while the
/// app was closed do not fire a notification storm. The file is still useful
/// as a turn-end audit ledger.
///
/// Uses the same 0.5s polling pattern as `KernelLogTailer` — good enough
/// at this rate and avoids dragging in FSEvents plumbing.
@MainActor
final class NotifyWatcher: NSObject, UNUserNotificationCenterDelegate {
    private let path: URL = FHS.root
        .appendingPathComponent("var/log/turns.jsonl")
    private var offset: UInt64 = 0
    private var timer: Timer?
    private var fileAppeared = false

    /// Called when the user taps a banner. AppDelegate sets this so we can
    /// route to the existing window instead of macOS launching a fresh
    /// process for an LSUIElement app.
    var onActivate: (() -> Void)?

    func start() {
        UNUserNotificationCenter.current().delegate = self
        UNUserNotificationCenter.current().requestAuthorization(
            options: [.alert, .sound]
        ) { _, _ in }

        // If the file already exists, jump to EOF; otherwise we'll attach
        // on the first poll that finds it.
        attachIfPresent()

        timer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.poll() }
        }
    }

    deinit { timer?.invalidate() }

    private func attachIfPresent() {
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: path.path),
              let size = attrs[.size] as? UInt64 else {
            return
        }
        offset = size
        fileAppeared = true
    }

    private func poll() {
        if !fileAppeared {
            attachIfPresent()
            return
        }
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: path.path),
              let size = attrs[.size] as? UInt64 else {
            return
        }
        if size < offset {
            // Truncated/rotated externally — restart from the top, but
            // still don't replay backlog: skip to current EOF.
            offset = size
            return
        }
        if size == offset { return }
        guard let handle = try? FileHandle(forReadingFrom: path) else { return }
        defer { try? handle.close() }
        do { try handle.seek(toOffset: offset) } catch { return }
        guard let chunk = try? handle.readToEnd(), !chunk.isEmpty else { return }
        offset = size
        guard let text = String(data: chunk, encoding: .utf8) else { return }
        for raw in text.split(separator: "\n", omittingEmptySubsequences: true) {
            handleLine(String(raw))
        }
    }

    private func handleLine(_ line: String) {
        guard let data = line.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let slug = obj["slug"] as? String else {
            return
        }
        let turnIndex = obj["turn_index"] as? Int ?? 0
        let content = UNMutableNotificationContent()
        content.title = "\(slug) is done!"
        // Body intentionally empty.
        let request = UNNotificationRequest(
            identifier: "pai-finished-\(slug)-\(turnIndex)",
            content: content,
            trigger: nil
        )
        UNUserNotificationCenter.current().add(request) { _ in }
    }

    // Show banner even while the app is foregrounded.
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }

    // Banner tap → bring the running window forward. Without this handler,
    // LaunchServices may spawn a second instance for LSUIElement apps.
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        Task { @MainActor in
            self.onActivate?()
            completionHandler()
        }
    }
}
