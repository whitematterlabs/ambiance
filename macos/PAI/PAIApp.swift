import SwiftUI
import Combine
import AppKit

/// Click the menubar icon → main window toggles. No dropdown.
///
/// We can't get that out of SwiftUI's `MenuBarExtra` (it always shows a
/// menu/popover). So the App scene is reduced to a settings shell, and an
/// `NSApplicationDelegate` owns the real surfaces:
///   - the `NSStatusItem` button (single-click target),
///   - the `NSWindow` hosting `MainWindow` via `NSHostingController`,
///   - the shared `@StateObject`-equivalents (registry, procs, log, state).
///
/// The window's red close button hides instead of destroying, so the next
/// click on the menubar icon brings it back instantly.
@main
struct PAIApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        // Settings scene is required to keep SwiftUI happy; we don't use it.
        Settings { Text("PAI — MVP").padding() }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    // Shared state. These match the @StateObject set the SwiftUI App used
    // to own — moved here because AppDelegate is now the long-lived root.
    private let registry = PAIRegistry()
    private let procs = ProcRegistry()
    private let log = KernelLogTailer()
    private let state = AppState()
    private let cloner = PAICloner()

    private var statusItem: NSStatusItem!
    private var window: NSWindow!
    private var iconSubscription: AnyCancellable?

    func applicationDidFinishLaunching(_ notification: Notification) {
        installStatusItem()
        buildMainWindow()
        observeRegistryForIcon()
    }

    private func installStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem = item
        if let button = item.button {
            button.image = currentIcon()
            button.imagePosition = .imageLeft
            button.title = ""
            button.target = self
            button.action = #selector(toggleWindow(_:))
            // Allow right-click → quit, since we lost the menu.
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
    }

    private func buildMainWindow() {
        let root = MainWindow(
            registry: registry, procs: procs, log: log, state: state, cloner: cloner
        )
        let hosting = NSHostingController(rootView: root)
        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 860, height: 560),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        win.title = "PAI"
        win.contentViewController = hosting
        win.isReleasedWhenClosed = false
        win.center()
        win.delegate = self
        win.collectionBehavior = [.fullScreenPrimary, .moveToActiveSpace]
        window = win
    }

    private func observeRegistryForIcon() {
        // Refresh the menubar icon whenever the registry's published values
        // change. CombineLatest keeps it cheap — one image swap per tick.
        iconSubscription = registry.objectWillChange
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in
                guard let self else { return }
                Task { @MainActor in
                    self.statusItem.button?.image = self.currentIcon()
                }
            }
    }

    private func currentIcon() -> NSImage? {
        let name: String
        if !registry.kernelOnline {
            name = "bubble.left.and.exclamationmark.bubble.right"
        } else if registry.pais.contains(where: { $0.busy != nil }) {
            name = "bubble.left.and.bubble.right.fill"
        } else {
            name = "bubble.left.and.bubble.right"
        }
        return NSImage(systemSymbolName: name, accessibilityDescription: "PAI")
    }

    @objc private func toggleWindow(_ sender: Any?) {
        // Right-click → quit. Matches the affordance we lost when the
        // dropdown menu went away.
        if let event = NSApp.currentEvent, event.type == .rightMouseUp {
            NSApp.terminate(nil)
            return
        }
        if window.isVisible && window.isKeyWindow {
            window.orderOut(nil)
            return
        }
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    // Red close button hides the window instead of destroying it, so the
    // next menubar click brings the same window (and selection) back.
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        return false
    }
}

/// The one persistent window. Sidebar on the left, detail on the right.
struct MainWindow: View {
    @ObservedObject var registry: PAIRegistry
    @ObservedObject var procs: ProcRegistry
    @ObservedObject var log: KernelLogTailer
    @ObservedObject var state: AppState
    @ObservedObject var cloner: PAICloner

    var body: some View {
        NavigationSplitView {
            Sidebar(
                registry: registry, procs: procs,
                cloner: cloner, selection: $state.selection
            )
        } detail: {
            detail
                .frame(minWidth: 520, minHeight: 420)
        }
        .navigationTitle(titleForSelection)
        .onAppear { ensureSelection() }
        .onChange(of: registry.pais) { _, _ in ensureSelection() }
        .alert(
            "Clone failed",
            isPresented: Binding(
                get: { cloner.lastError != nil },
                set: { if !$0 { cloner.lastError = nil } }
            ),
            presenting: cloner.lastError
        ) { _ in
            Button("OK", role: .cancel) { cloner.lastError = nil }
        } message: { msg in
            Text(msg)
        }
    }

    @ViewBuilder
    private var detail: some View {
        switch state.selection {
        case .activity:
            ActivityWindow(log: log)
        case .procs:
            ProcsWindow(procs: procs)
        case .pai(let pid):
            if let pai = registry.pais.first(where: { $0.pid == pid }) {
                ChatWindow(pai: pai).id(pai.pid)
            } else {
                emptyState
            }
        case .none:
            emptyState
        }
    }

    private var emptyState: some View {
        VStack(spacing: 8) {
            Image(systemName: "bubble.left.and.exclamationmark.bubble.right")
                .font(.system(size: 36))
                .foregroundStyle(.secondary)
            Text(registry.kernelOnline ? "Pick something from the sidebar." : "Kernel offline.")
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var titleForSelection: String {
        switch state.selection {
        case .activity: return "Activity"
        case .procs: return "Processes"
        case .pai(let pid):
            return registry.pais.first(where: { $0.pid == pid }).map { "\($0.slug) #\($0.pid)" } ?? "PAI"
        case .none: return "PAI"
        }
    }

    private func ensureSelection() {
        if let sel = state.selection {
            if case .pai(let pid) = sel,
               !registry.pais.contains(where: { $0.pid == pid }) {
                state.selection = registry.pais.first.map { .pai($0.pid) } ?? .activity
            }
            return
        }
        state.selection = registry.pais.first.map { .pai($0.pid) } ?? .activity
    }
}
