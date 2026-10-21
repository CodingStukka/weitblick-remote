import AppKit
import ConnectionStore
import KeyboardEngine
import KeyboardEngineCarbon
import os
import WeitblickKit
import SwiftUI

/// One session window (a tab next to the overview, or its own window). It keeps the window, the
/// view, the tab status, the key monitor and the shortcut tap for its whole life; every connection
/// attempt is a `LiveSession`, so signing in again or "Reconnect" stays in the same tab.
///
/// Agent sessions (opened over MCP, `agentDesktop` set) use the same controller, but their window
/// stays off screen until the user watches, the desktop has a fixed size, the clipboard is private
/// and nothing that needs the user (sign-in, certificate) is ever asked: it fails instead.
@MainActor
final class SessionWindowController: NSWindowController, NSWindowDelegate, NSMenuItemValidation, RDPSessionDelegate {
    let connectionID: UUID
    /// The id agents use for this session (`list_sessions`, tools).
    let agentSessionID: String
    /// Set for sessions an agent opened: their fixed desktop size.
    let agentDesktop: PixelSize?
    private var connection: Connection
    private let library: ConnectionLibrary
    private let activity: SessionActivity
    private let sessionView = SessionView(frame: NSRect(x: 0, y: 0, width: 800, height: 600))
    private let overlayView = NSHostingView(rootView: SessionOverlay(phase: .hidden, actions: .init(retry: {}, signIn: {}, closeTab: {})))
    private let statusIndicator = SessionStatusIndicator()
    private var live: LiveSession?
    /// Session menu changes (mode, ⌥ strategy, clipboard) last for this window, across reconnects.
    private var keyboardConfig: KeyboardConfig
    private var clipboardEnabled = true
    /// The credentials of the last attempt, so "Reconnect" works without a saved password.
    private var lastCredentials: Credentials?
    private var shortcutTap: SystemShortcutTap!
    private var keyMonitor: Any?
    private var appObservers: [NSObjectProtocol] = []
    private var pendingResize: DispatchWorkItem?
    private var pendingCertificate: CheckedContinuation<CertificateDecision, Never>?
    private var isClosing = false
    private(set) var status = SessionStatus.connecting
    /// Called once the window has closed.
    var onClose: (() -> Void)?
    /// The window has been shown (a tab or its own window); agent sessions start without.
    var wasPresented = false
    /// An agent controls this session (nil: nobody).
    private(set) var agentControl: AgentControl? {
        didSet { if agentControl != oldValue { agentControlChanged(from: oldValue) } }
    }
    /// The user took over or resumed the agent, or the window appeared (overview badge, agent host).
    var onAgentControlChange: (() -> Void)?
    /// Why an agent session could not connect or ended (certificate, missing password, …).
    private(set) var agentEndMessage: String?
    private var connectWaiters: [CheckedContinuation<String?, Never>] = []
    private let agentBanner = NSHostingView(rootView: AgentBanner(control: AgentControl(client: ""), takeOver: {}, resume: {}))

    /// Waiting time after the last window size change before asking the server to resize.
    private static let resizeDebounce: TimeInterval = 0.3
    private static let logger = Logger(subsystem: "nrw.neuhaus.weitblick-remote", category: "window")

    /// `agentDesktop`: an agent opens this session (windowless, fixed desktop size).
    init(connection: Connection, library: ConnectionLibrary, activity: SessionActivity, sessionID: String,
         agentDesktop: PixelSize? = nil) {
        self.connectionID = connection.id
        self.agentSessionID = sessionID
        self.agentDesktop = agentDesktop
        self.connection = connection
        self.library = library
        self.activity = activity
        self.keyboardConfig = connection.keyboard
        clipboardEnabled = agentDesktop == nil && connection.redirection.clipboard

        let screen = NSScreen.main ?? NSScreen.screens[0]
        let visible = screen.visibleFrame.size
        let contentSize: NSSize
        if let agentDesktop {
            // One point per desktop pixel when it fits, else scaled down evenly.
            let fit = min(1, visible.width * 0.9 / CGFloat(agentDesktop.width), visible.height * 0.85 / CGFloat(agentDesktop.height))
            contentSize = NSSize(width: (CGFloat(agentDesktop.width) * fit).rounded(),
                                 height: (CGFloat(agentDesktop.height) * fit).rounded())
        } else {
            contentSize = connection.display.contentSize(backingScale: screen.backingScaleFactor)
                ?? NSSize(width: min(1440, visible.width * 0.85).rounded(), height: min(900, visible.height * 0.85).rounded())
        }
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: contentSize),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = connection.name
        window.tabbingIdentifier = WindowCoordinator.tabbingIdentifier
        window.collectionBehavior = [.fullScreenPrimary]
        window.contentMinSize = NSSize(width: 400, height: 300)
        window.acceptsMouseMovedEvents = true
        window.isReleasedWhenClosed = false
        if agentDesktop == nil {
            window.setFrameAutosaveName("Session \(connection.id.uuidString)")
        } else {
            // Agent windows leave no trace in the defaults (frames, restoration).
            window.isRestorable = false
            window.center()
        }
        super.init(window: window)
        shortcutTap = SystemShortcutTap { [unowned self] event in consumeCapturedKey(event) }

        let container = NSView(frame: NSRect(origin: .zero, size: contentSize))
        for view in [sessionView, overlayView] as [NSView] {
            view.frame = container.bounds
            view.autoresizingMask = [.width, .height]
            container.addSubview(view)
        }
        agentBanner.isHidden = true
        agentBanner.autoresizingMask = [.minXMargin, .maxXMargin, .minYMargin]
        container.addSubview(agentBanner)
        window.contentView = container
        window.tab.accessoryView = statusIndicator
        window.delegate = self
        sessionView.willSendPointerPress = { [unowned self] in live?.keyboard.prepareForPointerEvent() }
        setStatus(.connecting)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }

    private var isConnected: Bool { live?.isConnected ?? false }
    var isAgentSession: Bool { agentDesktop != nil }
    /// The user's input reaches the session (no agent, or the user took over).
    private var acceptsLocalInput: Bool { agentControl.map(\.paused) ?? true }

    /// Call once the window is on screen (as a tab or on its own): its size decides the desktop size.
    /// Agent sessions start without a window.
    func start() {
        installKeyMonitor()
        observeApp()
        if !isAgentSession, connection.display.startFullscreen, let window, !window.styleMask.contains(.fullScreen) {
            window.toggleFullScreen(nil)
        }
        connectWithSavedCredentials()
    }

    /// Brings this session's tab or window to the front.
    func activate() {
        guard let window else { return }
        window.tabGroup?.selectedWindow = window
        window.makeKeyAndOrderFront(nil)
    }

    // MARK: Connecting

    private func reconnectWithLastCredentials() {
        if let lastCredentials { connect(lastCredentials) } else { connectWithSavedCredentials() }
    }

    private func connectWithSavedCredentials() {
        let password = library.password(for: connectionID)
        // With NLA the server checks the password before showing anything: ask first.
        if !connection.security.disableNLA, password == nil || connection.username.isEmpty {
            if isAgentSession {
                return agentSessionFailed(Self.missingCredentialsMessage(connection))
            }
            askForCredentials(message: nil)
            return
        }
        connect(Credentials(username: connection.username, domain: connection.domain, password: password ?? ""))
    }

    private func connect(_ credentials: Credentials, saveAfterLogon: Bool = false) {
        endLiveSession()
        agentEndMessage = nil
        if let latest = library.connection(with: connectionID) { connection = latest }
        setStatus(.connecting)
        showOverlay(.connecting(host: connection.host))
        let desktop = agentDesktop.map { RemoteDesktop(size: $0, scale: RemoteScale(desktop: 100, device: 100)) }
            ?? connection.display.desktop(forViewSize: sessionView.bounds.size, backingScale: window?.backingScaleFactor ?? 2)
        let configuration = SessionConfiguration(connection: connection, credentials: credentials, desktop: desktop,
                                                 macKeyboardLayout: .current,
                                                 supportDirectory: library.store.fileURL.deletingLastPathComponent())
        // Agent sessions never see the Mac's pasteboard: a private one, and no sync.
        let pasteboard = isAgentSession ? NSPasteboard(name: NSPasteboard.Name("nrw.neuhaus.weitblick-remote.agent.\(UUID().uuidString)")) : .general
        guard let live = LiveSession(configuration: configuration, keyboardConfig: keyboardConfig,
                                     clipboardEnabled: clipboardEnabled, pasteboard: pasteboard, delegate: self) else {
            if isAgentSession { return agentSessionFailed(String(localized: "The session couldn’t be created.")) }
            showFailure(String(localized: "The session couldn’t be created."))
            return
        }
        live.credentialsToSave = saveAfterLogon ? credentials : nil
        lastCredentials = credentials
        self.live = live
        sessionView.session = live.session
        live.connect()
    }

    private func askForCredentials(message: String?) {
        guard let window, window.attachedSheet == nil else { return }
        setStatus(.disconnected)
        showOverlay(.signedOut(message: message ?? String(localized: "No password is saved for this connection.")))
        weak var sheet: NSWindow? // weak: the sheet's own view holds this closure
        let view = SignInView(
            connectionName: connection.name, message: message, username: connection.username, domain: connection.domain,
            onConnect: { [weak self] credentials, save in
                if let sheet { window.endSheet(sheet) }
                self?.connect(credentials, saveAfterLogon: save)
            },
            onCancel: { if let sheet { window.endSheet(sheet) } })
        sheet = window.presentSheet(view)
    }

    /// The server accepted the credentials from the sign-in sheet: keep them if asked to.
    private func saveAcceptedCredentials(_ live: LiveSession) {
        guard let credentials = live.credentialsToSave else { return }
        live.credentialsToSave = nil
        do {
            try library.setPassword(credentials.password, for: connectionID)
        } catch {
            Self.logger.error("saving password failed: \(String(describing: error), privacy: .public)")
            presentAlert(String(localized: "Password not saved"),
                         String(localized: "The keychain didn’t accept the password. The session continues anyway."))
        }
        guard credentials.username != connection.username || credentials.domain != connection.domain else { return }
        Task {
            try? await library.modify(connectionID) { stored in
                stored.username = credentials.username
                stored.domain = credentials.domain
            }
        }
    }

    // MARK: RDPSessionDelegate

    func session(_ session: RDPSession, didReceive event: RDPSessionEvent) {
        guard let live, session === live.session else { return }
        switch event {
        case .connected:
            sessionDidConnect(live)
            saveAcceptedCredentials(live)
            Task { await library.markConnected(connectionID) }
        case .reconnecting(let attempt):
            setStatus(.reconnecting(attempt: attempt))
            showOverlay(.reconnecting(attempt: attempt))
            live.keyboard.focusLost()
            updateShortcutCapture()
        case .reconnected:
            sessionDidConnect(live)
        case .disconnected:
            sessionEnded(reason: session.disconnectReason ?? .other)
        case .frameReady:
            sessionView.setNeedsPresent()
        case .desktopResized(let size):
            Self.logger.notice("desktop resized to \(size.description, privacy: .public)")
            sessionView.setNeedsPresent()
        case .pointer(let pointer):
            sessionView.apply(pointer)
        @unknown default:
            break
        }
    }

    func session(_ session: RDPSession, decideAbout certificate: ServerCertificate) async -> CertificateDecision {
        // An agent never decides about a certificate: the user connects once by hand.
        if isAgentSession {
            agentEndMessage = Self.certificateMessage(connection, changed: certificate.changed)
            return .reject
        }
        guard session === live?.session, !isClosing, let window, pendingCertificate == nil else { return .reject }
        let details = CertificateDetails(certificate)
        let decision = await withCheckedContinuation { continuation in
            pendingCertificate = continuation
            weak var sheet: NSWindow? // weak: the sheet's own view holds this closure
            sheet = window.presentSheet(CertificateView(certificate: details, connectionName: connection.name) { [weak self] decision in
                if let sheet { window.endSheet(sheet) }
                self?.answerCertificate(decision)
            })
        }
        if decision == .acceptPermanently {
            do {
                try await library.trust(fingerprint: certificate.fingerprint, for: connectionID, replacing: certificate.changed)
            } catch {
                Self.logger.error("storing the certificate failed: \(String(describing: error), privacy: .public)")
            }
        }
        return decision
    }

    private func answerCertificate(_ decision: CertificateDecision) {
        let continuation = pendingCertificate
        pendingCertificate = nil
        continuation?.resume(returning: decision)
    }

    private func sessionDidConnect(_ live: LiveSession) {
        setStatus(.connected)
        showOverlay(.hidden)
        window?.makeFirstResponder(sessionView)
        if window?.isKeyWindow == true, acceptsLocalInput { live.keyboard.focusGained() }
        if NSApp.isActive, !isAgentSession { live.clipboard.startWatching() }
        if agentControl?.paused == false { live.clipboard.isEnabled = false }
        updateShortcutCapture()
        scheduleResolutionUpdate() // the window may have changed while connecting
        resumeConnectWaiters(nil)
    }

    private func sessionEnded(reason: DisconnectReason) {
        endLiveSession()
        updateShortcutCapture()
        if isAgentSession {
            let message = agentEndMessage
                ?? (reason.needsCredentials ? Self.rejectedCredentialsMessage(connection) : reason.message)
            return agentSessionFailed(message, deliberate: reason.isDeliberate)
        }
        guard !isClosing, let window else { return }
        if let sheet = window.attachedSheet { window.endSheet(sheet) }
        answerCertificate(.reject)
        if reason.isDeliberate {
            window.close()
        } else if reason.needsCredentials {
            askForCredentials(message: reason.message)
        } else {
            showFailure(reason.message)
        }
    }

    private func showFailure(_ message: String) {
        setStatus(.disconnected)
        showOverlay(.failed(message: message))
    }

    /// Lets go of the current attempt (if any); the window and its last picture stay.
    private func endLiveSession() {
        guard let ended = live else { return }
        live = nil
        sessionView.session = nil
        ended.end()
    }

    // MARK: Agent sessions

    /// Waits until the session is connected (nil) or failed (the reason); at most `timeout`.
    func waitUntilConnected(timeout: Duration) async -> String? {
        if isConnected { return nil }
        if let agentEndMessage, live == nil { return agentEndMessage }
        let timer = Task { [weak self] in
            try? await Task.sleep(for: timeout)
            guard !Task.isCancelled else { return }
            self?.resumeConnectWaiters("The connection to “\(self?.connection.name ?? "")” took longer than \(Int(timeout.components.seconds)) seconds.")
        }
        defer { timer.cancel() }
        return await withCheckedContinuation { connectWaiters.append($0) }
    }

    private func resumeConnectWaiters(_ message: String?) {
        let waiters = connectWaiters
        connectWaiters = []
        waiters.forEach { $0.resume(returning: message) }
    }

    /// An agent session could not connect or ended: tell the waiting tool, then let the window go
    /// unless the user watches it.
    private func agentSessionFailed(_ message: String, deliberate: Bool = false) {
        agentEndMessage = message
        endLiveSession()
        resumeConnectWaiters(message)
        guard !isClosing, let window else { return }
        if deliberate || !window.isVisible {
            window.close()
        } else {
            showFailure(message)
        }
    }

    /// The connected RDP session (nil while connecting, reconnecting or after the end).
    var connectedSession: RDPSession? { isConnected ? live?.session : nil }
    var clipboard: ClipboardSync? { live?.clipboard }
    var currentConnection: Connection { connection }
    var isWindowVisible: Bool { window?.isVisible ?? false }

    /// An agent takes control (user session: attach). Local input is blocked until the user takes over.
    func attachAgent(client: String) {
        agentControl = AgentControl(client: client)
    }

    /// The agent lets go (disconnect, client gone, idle).
    func detachAgent() {
        agentControl = nil
    }

    func takeOverFromAgent() {
        agentControl?.paused = true
    }

    func resumeAgent() {
        agentControl?.paused = false
    }

    private func agentControlChanged(from old: AgentControl?) {
        sessionView.acceptsLocalInput = acceptsLocalInput
        if let live {
            if !acceptsLocalInput {
                // The user's keys must not stay down while the agent types.
                live.keyboard.focusLost()
                live.clipboard.isEnabled = false
            } else {
                live.clipboard.isEnabled = clipboardEnabled
                if window?.isKeyWindow == true { live.keyboard.focusGained() }
            }
        }
        updateAgentBanner()
        updateShortcutCapture()
        publishAgentActivity()
        onAgentControlChange?()
    }

    private func updateAgentBanner() {
        guard let agentControl else {
            agentBanner.isHidden = true
            return
        }
        agentBanner.rootView = AgentBanner(
            control: agentControl,
            takeOver: { [weak self] in self?.takeOverFromAgent() },
            resume: { [weak self] in self?.resumeAgent() })
        let size = agentBanner.fittingSize
        if let container = window?.contentView {
            agentBanner.frame = NSRect(x: ((container.bounds.width - size.width) / 2).rounded(),
                                       y: container.bounds.height - size.height, width: size.width, height: size.height)
        }
        agentBanner.isHidden = false
    }

    /// The overview's badge and "Watch" button.
    func publishAgentActivity() {
        guard !isClosing else { return }
        let agent = agentControl.map {
            AgentActivity(client: $0.client, paused: $0.paused, windowless: !(window?.isVisible ?? false))
        }
        activity.setAgent(agent, for: connectionID)
    }

    func windowDidChangeOcclusionState(_ notification: Notification) {
        publishAgentActivity()
    }

    // Messages for agents: English whatever the UI language (the model reads them).

    private static func missingCredentialsMessage(_ connection: Connection) -> String {
        "No user name or password is saved for “\(connection.name)”. Ask the user to connect once by hand in Weitblick Remote and save the password."
    }

    private static func rejectedCredentialsMessage(_ connection: Connection) -> String {
        "Windows rejected the saved user name or password for “\(connection.name)”. Ask the user to connect once by hand in Weitblick Remote and save the right password."
    }

    private static func certificateMessage(_ connection: Connection, changed: Bool) -> String {
        changed
            ? "The certificate of “\(connection.name)” has changed. Ask the user to connect once by hand in Weitblick Remote and check it."
            : "The certificate of “\(connection.name)” isn’t trusted yet. Ask the user to connect once by hand in Weitblick Remote and trust it."
    }

    // MARK: Status

    private func setStatus(_ status: SessionStatus) {
        self.status = status
        statusIndicator.show(status)
        window?.subtitle = status == .connected ? "" : status.label
        activity.set(status, for: connectionID)
    }

    private func showOverlay(_ phase: SessionOverlayPhase) {
        overlayView.rootView = SessionOverlay(phase: phase, actions: SessionOverlayActions(
            retry: { [weak self] in self?.reconnectWithLastCredentials() },
            signIn: { [weak self] in self?.askForCredentials(message: nil) },
            closeTab: { [weak self] in self?.window?.close() }))
        overlayView.isHidden = phase == .hidden
    }

    private func presentAlert(_ title: String, _ text: String) {
        guard let window else { return }
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = text
        alert.beginSheetModal(for: window)
    }

    // MARK: Keyboard

    private func installKeyMonitor() {
        // A local monitor sees every key event of the app, including key-ups AppKit swallows
        // while ⌘ is held, before menus get key equivalents.
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown, .keyUp, .flagsChanged]) { [weak self] incoming in
            nonisolated(unsafe) let event = incoming
            let consumed = MainActor.assumeIsolated { self?.forwardToSession(event) ?? false }
            return consumed ? nil : incoming
        }
    }

    /// Sends a key event of this window to the session unless the engine keeps it for the app
    /// (⌃⌘F, ⌃⌥⌘… such as the tab shortcuts).
    private func forwardToSession(_ event: NSEvent) -> Bool {
        guard event.window === window, let live, live.isConnected, let keyEvent = KeyEvent(event) else { return false }
        // While an agent types, the user's keys go nowhere (⌘ shortcuts still reach the menus).
        guard acceptsLocalInput else { return !event.modifierFlags.contains(.command) }
        return live.keyboard.handle(keyEvent)
    }

    /// Events from the shortcut tap. Reserved shortcuts pass on to AppKit through the monitor.
    private func consumeCapturedKey(_ event: CGEvent) -> Bool {
        guard shouldCaptureSystemShortcuts, let live else {
            DispatchQueue.main.async { [weak self] in self?.updateShortcutCapture() }
            return false
        }
        guard let keyEvent = KeyEvent(event), !live.keyboard.isReserved(keyEvent) else { return false }
        return live.keyboard.handle(keyEvent)
    }

    private var shouldCaptureSystemShortcuts: Bool {
        guard isConnected, acceptsLocalInput, let window else { return false }
        return SystemShortcutCapture.setting.applies(
            sessionIsKey: window.isKeyWindow, appIsActive: NSApp.isActive,
            isFullScreen: window.styleMask.contains(.fullScreen))
    }

    private func updateShortcutCapture() {
        if shouldCaptureSystemShortcuts {
            if !shortcutTap.start() { Self.logger.notice("system shortcuts stay local: no Accessibility permission") }
        } else {
            shortcutTap.stop()
        }
    }

    private func observeApp() {
        let center = NotificationCenter.default
        let names: [Notification.Name] = [
            NSApplication.didBecomeActiveNotification, NSApplication.didResignActiveNotification, .systemShortcutCaptureChanged,
        ]
        appObservers = names.map { name in
            center.addObserver(forName: name, object: nil, queue: .main) { [weak self] notification in
                let name = notification.name
                MainActor.assumeIsolated { self?.appStateChanged(name) }
            }
        }
    }

    private func appStateChanged(_ name: Notification.Name) {
        switch name {
        case NSApplication.didBecomeActiveNotification:
            guard let live, live.isConnected, !isAgentSession else { break }
            live.clipboard.checkPasteboard()
            live.clipboard.startWatching()
        case NSApplication.didResignActiveNotification:
            live?.clipboard.stopWatching()
        default:
            break
        }
        updateShortcutCapture()
    }

    /// A tab switch makes another window key: this one resigns and lets go of every key.
    func windowDidBecomeKey(_ notification: Notification) {
        guard let live, live.isConnected, acceptsLocalInput else { return }
        live.keyboard.focusGained()
        live.clipboard.checkPasteboard()
        updateShortcutCapture()
    }

    func windowDidResignKey(_ notification: Notification) {
        updateShortcutCapture()
        guard let live, live.isConnected, acceptsLocalInput else { return }
        live.keyboard.focusLost()
    }

    func windowDidEnterFullScreen(_ notification: Notification) { updateShortcutCapture() }
    func windowDidExitFullScreen(_ notification: Notification) { updateShortcutCapture() }

    // MARK: Session menu

    private static let ctrlAltDelete = try! WindowsChord("ctrl+alt+delete")
    private static let windowsKey = try! WindowsChord("win")
    private static let altTab = try! WindowsChord("alt+tab")
    private static let printScreen = try! WindowsChord("printscreen")

    @objc func sendCtrlAltDelete(_ sender: Any?) { live?.keyboard.tap(Self.ctrlAltDelete) }
    @objc func sendWindowsKey(_ sender: Any?) { live?.keyboard.tap(Self.windowsKey) }
    @objc func sendAltTab(_ sender: Any?) { live?.keyboard.tap(Self.altTab) }
    @objc func sendPrintScreen(_ sender: Any?) { live?.keyboard.tap(Self.printScreen) }

    @objc func selectKeyboardMode(_ sender: NSMenuItem) {
        keyboardConfig.mode = SessionMenu.keyboardModes[sender.tag]
        live?.keyboard.apply(keyboardConfig)
    }

    @objc func selectOptionStrategy(_ sender: NSMenuItem) {
        keyboardConfig.optionStrategy = SessionMenu.optionStrategies[sender.tag]
        live?.keyboard.apply(keyboardConfig)
    }

    @objc func toggleClipboardSync(_ sender: Any?) {
        clipboardEnabled.toggle()
        live?.clipboard.isEnabled = clipboardEnabled
    }

    @objc func selectSystemShortcutCapture(_ sender: NSMenuItem) {
        SystemShortcutCapture.setting = SystemShortcutCapture.allCases[sender.tag]
        NotificationCenter.default.post(name: .systemShortcutCaptureChanged, object: nil)
    }

    @objc func reconnect(_ sender: Any?) {
        reconnectWithLastCredentials()
    }

    func validateMenuItem(_ item: NSMenuItem) -> Bool {
        switch item.action {
        case #selector(selectKeyboardMode(_:)):
            item.state = SessionMenu.keyboardModes[item.tag] == keyboardConfig.mode ? .on : .off
            return true
        case #selector(selectOptionStrategy(_:)):
            item.state = SessionMenu.optionStrategies[item.tag] == keyboardConfig.optionStrategy ? .on : .off
            return keyboardConfig.mode == .macShortcuts
        case #selector(toggleClipboardSync(_:)):
            item.state = clipboardEnabled ? .on : .off
            return connection.redirection.clipboard && !isAgentSession
        case #selector(selectSystemShortcutCapture(_:)):
            item.state = SystemShortcutCapture.allCases[item.tag] == SystemShortcutCapture.setting ? .on : .off
            return true
        case #selector(reconnect(_:)):
            return status == .disconnected && window?.attachedSheet == nil && !isAgentSession
        default:
            return isConnected && acceptsLocalInput
        }
    }

    // MARK: Resolution

    func windowDidResize(_ notification: Notification) { scheduleResolutionUpdate() }
    func windowDidChangeBackingProperties(_ notification: Notification) { scheduleResolutionUpdate() }

    private func scheduleResolutionUpdate() {
        pendingResize?.cancel()
        guard !isAgentSession, connection.display.resizesWithWindow else { return }
        let work = DispatchWorkItem { [weak self] in
            MainActor.assumeIsolated { self?.sendResolution() }
        }
        pendingResize = work
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.resizeDebounce, execute: work)
    }

    private func sendResolution() {
        guard let live, live.isConnected, let window else { return }
        let desktop = connection.display.desktop(forViewSize: sessionView.bounds.size, backingScale: window.backingScaleFactor)
        Self.logger.notice("requesting remote size \(desktop.size.description, privacy: .public)")
        live.session.setResolution(desktop.size, scale: desktop.scale)
    }

    // MARK: Closing

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        guard live != nil, AppSettings.confirmCloseSession else { return true }
        let alert = NSAlert()
        alert.messageText = String(localized: "Disconnect from “\(connection.name)”?")
        alert.informativeText = isAgentSession
            ? String(localized: "The agent loses this session. Open programs keep running in Windows.")
            : String(localized: "The tab closes. Open programs keep running in Windows.")
        alert.addButton(withTitle: String(localized: "Disconnect"))
        alert.addButton(withTitle: String(localized: "Cancel"))
        alert.showsSuppressionButton = true
        alert.suppressionButton?.title = String(localized: "Don’t ask again")
        alert.beginSheetModal(for: sender) { response in
            guard response == .alertFirstButtonReturn else { return }
            if alert.suppressionButton?.state == .on { AppSettings.confirmCloseSession = false }
            sender.close()
        }
        return false
    }

    func windowWillClose(_ notification: Notification) {
        isClosing = true
        pendingResize?.cancel()
        answerCertificate(.reject)
        if agentEndMessage == nil, isAgentSession {
            agentEndMessage = "The session “\(connection.name)” was closed."
        }
        resumeConnectWaiters(agentEndMessage ?? "The session “\(connection.name)” was closed.")
        if let keyMonitor { NSEvent.removeMonitor(keyMonitor) }
        keyMonitor = nil
        appObservers.forEach(NotificationCenter.default.removeObserver)
        appObservers = []
        shortcutTap.stop()
        endLiveSession()
        sessionView.tearDown()
        activity.set(nil, for: connectionID)
        onClose?()
    }
}

extension NSWindow {
    /// Shows a SwiftUI view as a sheet of this window; end it with `endSheet(_:)`.
    @discardableResult
    func presentSheet(_ view: some View) -> NSWindow {
        let controller = NSHostingController(rootView: view)
        controller.sizingOptions = .preferredContentSize
        let sheet = NSWindow(contentViewController: controller)
        beginSheet(sheet)
        return sheet
    }
}

extension Notification.Name {
    static let systemShortcutCaptureChanged = Notification.Name("nrw.neuhaus.weitblick-remote.systemShortcutCaptureChanged")
}
