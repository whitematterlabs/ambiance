import AppKit
import WebKit

/// Owns the single `NSWindow` that hosts the React web UI in a `WKWebView`,
/// loaded via the `pai://` scheme (no loopback TCP). Toggle visibility from
/// the menubar status item.
///
/// The window is built lazily on first toggle, so a cold launch doesn't pay
/// for WKWebView until the user actually clicks the menubar icon. Closing
/// the red traffic-light just hides the window so the next click brings the
/// same webview back (preserving its scroll/route state).
@MainActor
final class WebWindowController: NSObject, NSWindowDelegate {
    private let socketPath: String
    private var window: NSWindow?
    private var webView: WKWebView?
    private var handler: PAIWebSchemeHandler?

    init(socketPath: String) {
        self.socketPath = socketPath
    }

    func toggle() {
        if let win = window, win.isVisible, win.isKeyWindow {
            win.orderOut(nil)
            return
        }
        if window == nil { buildWindow() }
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    func show() {
        if window == nil { buildWindow() }
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    /// Reload from the entry point. Used after `pai_web` is (re)started so a
    /// blank-page race recovers without quitting the app.
    func reload() {
        guard let webView else { return }
        webView.load(URLRequest(url: URL(string: "pai://app/")!))
    }

    private func buildWindow() {
        let schemeHandler = PAIWebSchemeHandler(socketPath: socketPath)
        let config = WKWebViewConfiguration()
        config.setURLSchemeHandler(schemeHandler, forURLScheme: PAIWebSchemeHandler.scheme)
        config.websiteDataStore = .default()
        // Audio playback (TTS) needs to autoplay without user gesture once the
        // user has interacted with the chat — same model as a website.
        config.mediaTypesRequiringUserActionForPlayback = []

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.allowsBackForwardNavigationGestures = false
        if #available(macOS 13.3, *) {
            webView.isInspectable = true  // dev convenience; harmless in ship builds
        }

        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1240, height: 760),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        win.title = "PAI"
        win.titleVisibility = .visible
        win.isReleasedWhenClosed = false
        win.contentView = webView
        win.center()
        win.delegate = self
        win.collectionBehavior = [.fullScreenPrimary, .moveToActiveSpace]

        self.handler = schemeHandler
        self.webView = webView
        self.window = win

        webView.load(URLRequest(url: URL(string: "pai://app/")!))
    }

    // Red-button hide-don't-destroy: next menubar click brings the same window
    // back with its state intact.
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        return false
    }
}
