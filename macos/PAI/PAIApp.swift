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
    private let launcher = KernelLauncher()
    private let notifier = NotifyWatcher()
    private let events = EventsTailer()
    private let provisioner = Provisioner()
    private let modelSetup = ModelSetup()
    private let ngrokSetup = NgrokSetup()
    private let loginItem = LoginItem()
    private let capabilities = CapabilityCatalog()
    private let webLauncher = WebServerLauncher()
    private let remoteAccess = RemoteAccess()

    private var statusItem: NSStatusItem!
    private var window: NSWindow!
    private var webWindow: WebWindowController?
    private var setupWindow: NSWindow?
    private var modelSetupWindow: NSWindow?
    private var ngrokSetupWindow: NSWindow?
    private var capabilitiesWindow: NSWindow?
    private var iconSubscription: AnyCancellable?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // The status-bar "Add capabilities…" item can fire any time post-launch.
        NotificationCenter.default.addObserver(
            forName: .openCapabilities, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.openCapabilities() }
        }
        // Shipped build, first run (or schema bump): provision ~/.pai behind a
        // setup window before bringing up the real surfaces. Dev builds (no
        // bundled runtime) report needsProvisioning == false and skip straight
        // to the normal app.
        if provisioner.needsProvisioning {
            // Before laying out the FHS, ask which model to boot on and for its
            // API key (install.sh's interactive prompts, as a GUI step). Skip
            // it when a provider key is already reachable — that provider then
            // becomes the seed default. PAI can't run without a key, so this
            // gates provisioning on a clean install.
            if modelSetup.needsSetup {
                runModelSetup()
            } else {
                modelSetup.selected = modelSetup.skipDefault
                runFirstRunProvisioning()
            }
        } else {
            // Plain launch of an already-set-up root: come up to the menubar
            // without popping the window (matches the original behavior).
            continueAfterProvisioning(activate: false)
        }
    }

    /// First-run model/provider picker + API-key entry. On continue we tear the
    /// window down and move on to provisioning, which seeds config.yaml on the
    /// chosen provider; the key is written to `~/.pai/.env` after the FHS exists.
    private func runModelSetup() {
        let view = ModelSetupView(setup: modelSetup) { [weak self] in
            guard let self else { return }
            self.modelSetupWindow?.orderOut(nil)
            self.modelSetupWindow = nil
            self.runFirstRunProvisioning()
        }
        let hosting = NSHostingController(rootView: view)
        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 520, height: 460),
            styleMask: [.titled, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        win.title = "PAI Setup"
        win.titlebarAppearsTransparent = true
        win.isReleasedWhenClosed = false
        win.contentViewController = hosting
        win.center()
        modelSetupWindow = win
        NSApp.activate(ignoringOtherApps: true)
        win.makeKeyAndOrderFront(nil)
    }

    /// Once the root is provisioned (confirmed or freshly laid out), run the
    /// one-time capability picker if it hasn't run yet, then start the app.
    /// `activate` brings the window forward when we just finished a first-run
    /// step; a plain cold launch leaves it in the menubar. Dev builds (no
    /// bundled runtime) report needsSetup == false and skip the picker.
    private func continueAfterProvisioning(activate: Bool) {
        if capabilities.needsSetup {
            runCapabilitySetup(firstRun: true)
        } else {
            proceedAfterCapabilities(activate: activate)
        }
    }

    /// Last first-run gate before the app proper: offer the optional ngrok
    /// authtoken step so remote access works later without a terminal trip.
    /// Skipped on dev builds and once the user has answered it once.
    private func proceedAfterCapabilities(activate: Bool) {
        if ngrokSetup.needsFirstRunSetup {
            runNgrokSetup(activate: activate)
        } else {
            startNormalSurfaces()
            if activate { activateWindow() }
        }
    }

    /// Optional first-run remote-access step. Save or "Set up later" both tear
    /// the window down and continue into the app; saving stores the authtoken
    /// via ngrok so the StatusBar toggle works immediately.
    private func runNgrokSetup(activate: Bool) {
        let proceed: () -> Void = { [weak self] in
            guard let self else { return }
            self.ngrokSetup.markAsked()
            self.ngrokSetupWindow?.orderOut(nil)
            self.ngrokSetupWindow = nil
            self.startNormalSurfaces()
            if activate { self.activateWindow() }
        }
        let view = NgrokSetupView(
            setup: ngrokSetup, onSaved: proceed, onSkip: proceed
        )
        let hosting = NSHostingController(rootView: view)
        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 480, height: 300),
            styleMask: [.titled, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        win.title = "PAI Setup"
        win.titlebarAppearsTransparent = true
        win.isReleasedWhenClosed = false
        win.contentViewController = hosting
        win.center()
        ngrokSetupWindow = win
        NSApp.activate(ignoringOtherApps: true)
        win.makeKeyAndOrderFront(nil)
    }

    /// Stand up the menubar item, web window, watchers, and FDA prompt. Called
    /// directly on a provisioned root, or after first-run provisioning succeeds.
    /// The menubar click opens a native window hosting the React web UI via a
    /// `pai://` scheme handler that proxies HTTP over a unix socket — no
    /// loopback TCP listener, leaving the port free for a future ngrok-tunneled
    /// remote surface.
    private func startNormalSurfaces() {
        installStatusItem()
        startWebServerAndWindow()
        observeRegistryForIcon()
        notifier.onActivate = { [weak self] in self?.activateWindow() }
        notifier.start()
        events.start()
        promptForFullDiskAccessIfNeeded()
    }

    private func startWebServerAndWindow() {
        webLauncher.start()
        let socketPath = webLauncher.socketURL.path
        webWindow = WebWindowController(socketPath: socketPath)
        // Best-effort: warm-load the window once the socket is up. This avoids
        // a "connect failed" flash if the user clicks the menubar within ~1s
        // of launch. Failing silently is fine — first click will load anyway.
        Task { @MainActor in
            await webLauncher.waitForReady()
        }
    }

    /// Show the setup window and kick off provisioning. On success we tear the
    /// setup window down and continue into `startNormalSurfaces()`; on failure
    /// the window stays up with a Retry button.
    private func runFirstRunProvisioning() {
        let view = SetupWindow(provisioner: provisioner) { [weak self] in
            self?.startProvisioning()
        }
        let hosting = NSHostingController(rootView: view)
        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 520, height: 400),
            styleMask: [.titled, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        win.title = "PAI Setup"
        win.titlebarAppearsTransparent = true
        win.isReleasedWhenClosed = false
        win.contentViewController = hosting
        win.center()
        setupWindow = win
        NSApp.activate(ignoringOtherApps: true)
        win.makeKeyAndOrderFront(nil)
        startProvisioning()
    }

    private func startProvisioning() {
        Task { @MainActor in
            await provisioner.provision(
                provider: modelSetup.selected.id, model: modelSetup.selected.model
            )
            guard provisioner.lastError == nil else { return }  // Retry stays available.
            // FHS now exists → persist the chosen provider's key (no-op if the
            // user skipped or a key was already reachable).
            modelSetup.commitKey()
            setupWindow?.orderOut(nil)
            setupWindow = nil
            // Freshly provisioned → run the first-run capability picker (then
            // the app), or go straight in if it somehow already ran.
            continueAfterProvisioning(activate: true)
        }
    }

    /// First-run capability picker (after provisioning) or an on-demand reopen
    /// from the menu. On first run we have no close button — the user concludes
    /// with Install or Skip; `onDone` then brings up the app. A menu reopen is
    /// closable and just dismisses.
    private func runCapabilitySetup(firstRun: Bool) {
        let view = SetupCapabilitiesView(catalog: capabilities, firstRun: firstRun) { [weak self] in
            guard let self else { return }
            self.capabilitiesWindow?.orderOut(nil)
            self.capabilitiesWindow = nil
            if firstRun {
                self.proceedAfterCapabilities(activate: true)
            }
        }
        let hosting = NSHostingController(rootView: view)
        var style: NSWindow.StyleMask = [.titled, .fullSizeContentView]
        if !firstRun { style.insert(.closable) }
        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 580, height: 560),
            styleMask: style, backing: .buffered, defer: false
        )
        win.title = "PAI Capabilities"
        win.titlebarAppearsTransparent = true
        win.isReleasedWhenClosed = false
        win.contentViewController = hosting
        win.center()
        win.delegate = self
        capabilitiesWindow = win
        NSApp.activate(ignoringOtherApps: true)
        win.makeKeyAndOrderFront(nil)
    }

    /// Status-bar "Add capabilities…". Reuse an open window if present;
    /// otherwise reload fresh installed-state and show the picker.
    private func openCapabilities() {
        if let win = capabilitiesWindow {
            NSApp.activate(ignoringOtherApps: true)
            win.makeKeyAndOrderFront(nil)
            return
        }
        capabilities.prepareForReopen()
        runCapabilitySetup(firstRun: false)
    }

    /// macOS has no API to *prompt* for Full Disk Access — apps detect the
    /// missing grant and deep-link the user into System Settings. We probe a
    /// TCC-protected path (Safari bookmarks); if the read fails, FDA is not
    /// granted for this bundle and we surface an alert on every launch.
    private func promptForFullDiskAccessIfNeeded() {
        guard !hasFullDiskAccess() else { return }
        let alert = NSAlert()
        alert.messageText = "PAI needs Full Disk Access"
        alert.informativeText = """
        PAI reads and writes across your home directory (mail, calendar caches, notes, \
        and the PAI filesystem). Grant Full Disk Access in System Settings, then \
        relaunch PAI.
        """
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Open System Settings")
        alert.addButton(withTitle: "Later")
        if alert.runModal() == .alertFirstButtonReturn {
            if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles") {
                NSWorkspace.shared.open(url)
            }
        }
    }

    private func hasFullDiskAccess() -> Bool {
        // Probe the per-user TCC database: it exists on every macOS account and
        // is itself gated behind Full Disk Access, so a successful read means
        // FDA is granted and a failure means it isn't. The old probe read
        // Safari bookmarks and treated ENOENT (file absent — e.g. Safari never
        // run) as "granted" — a false-positive that silently skipped the prompt
        // and left FDA-dependent drivers (mail, messages) failing with no nudge.
        let probe = ("~/Library/Application Support/com.apple.TCC/TCC.db" as NSString).expandingTildeInPath
        let fd = open(probe, O_RDONLY)
        if fd >= 0 { close(fd); return true }
        return false
    }

    private func activateWindow() {
        // Nil while the first-run setup window is up; nothing to bring forward.
        guard let webWindow else { return }
        webWindow.show()
    }

    // The app OWNS the kernel and the web server: when PAI quits, both quit
    // with it. We SIGTERM the kernel and block briefly so its PAIs shut down
    // cleanly; pai_web is a thin attach-only process and can be torn down
    // after.
    func applicationWillTerminate(_ notification: Notification) {
        launcher.terminateKernelSync()
        // Take the public tunnel down before the local children it fronts.
        remoteAccess.terminateSync()
        webLauncher.terminateSync()
    }

    // If macOS asks to "reopen" the app (Dock click, notification tap on
    // some paths), surface the window instead of doing nothing.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        activateWindow()
        return true
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
            registry: registry, procs: procs, log: log, state: state,
            cloner: cloner, launcher: launcher, events: events, loginItem: loginItem,
            remote: remoteAccess
        )
        let hosting = NSHostingController(rootView: root)
        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 920, height: 600),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        win.title = "PAI"
        win.titlebarAppearsTransparent = true
        win.titleVisibility = .visible
        win.styleMask.insert(.unifiedTitleAndToolbar)
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
        webWindow?.toggle()
    }

    // Red close button hides the window instead of destroying it, so the
    // next menubar click brings the same window (and selection) back. The
    // capability window is the exception: it genuinely closes (and clears its
    // reference) so a later reopen builds a fresh one.
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if sender == capabilitiesWindow {
            capabilitiesWindow = nil
            return true
        }
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
    @ObservedObject var launcher: KernelLauncher
    @ObservedObject var events: EventsTailer
    @ObservedObject var loginItem: LoginItem
    @ObservedObject var remote: RemoteAccess

    var body: some View {
        VStack(spacing: 0) {
            NavigationSplitView {
                Sidebar(
                    registry: registry, procs: procs,
                    cloner: cloner, selection: $state.selection
                )
            } detail: {
                detail
                    .frame(minWidth: 520, minHeight: 420)
            }
            Divider()
            StatusBar(registry: registry, launcher: launcher, loginItem: loginItem, remote: remote, selection: state.selection)
        }
        .navigationTitle(titleForSelection)
        .onAppear {
            ensureSelection()
        }
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
        .alert(
            "Kernel launch failed",
            isPresented: Binding(
                get: { launcher.lastError != nil },
                set: { if !$0 { launcher.lastError = nil } }
            ),
            presenting: launcher.lastError
        ) { _ in
            Button("OK", role: .cancel) { launcher.lastError = nil }
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
                ChatWindow(pai: pai, events: events).id(pai.pid)
            } else {
                emptyState
            }
        case .none:
            emptyState
        }
    }

    private var emptyState: some View {
        let online = registry.kernelOnline
        return VStack(spacing: 14) {
            Image(systemName: online
                  ? "bubble.left.and.bubble.right"
                  : "bubble.left.and.exclamationmark.bubble.right")
                .font(.system(size: 52, weight: .light))
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(online ? Color.accentColor : Color.secondary)
            VStack(spacing: 4) {
                Text(online ? "PAI" : "Kernel offline")
                    .font(.title2.weight(.semibold))
                Text(online
                     ? "Pick a PAI from the sidebar, or open Activity to watch the kernel."
                     : "No kernel running. Start it to bring PAIs online.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 360)
            }
            if !online {
                VStack(spacing: 10) {
                    Button {
                        launcher.start()
                    } label: {
                        Text("Start kernel")
                            .frame(minWidth: 140)
                    }
                    .controlSize(.large)
                    .buttonStyle(.borderedProminent)
                    .disabled(launcher.inFlight)
                }
                .padding(.top, 4)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(
            LinearGradient(
                colors: [
                    Color(nsColor: .windowBackgroundColor),
                    Color(nsColor: .underPageBackgroundColor)
                ],
                startPoint: .top, endPoint: .bottom
            )
        )
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
