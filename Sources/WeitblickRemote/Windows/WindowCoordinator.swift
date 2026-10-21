import AppKit
import ConnectionStore
import os
import WeitblickKit

/// Owns the overview and the session windows: opens sessions as native tabs next to the overview
/// (or as own windows), switches tabs, opens files and decides about quitting.
@MainActor
final class WindowCoordinator: NSObject, NSMenuItemValidation, NSMenuDelegate {
    /// Shared by the overview and every session window so they form one tab group.
    static let tabbingIdentifier = "nrw.neuhaus.weitblick-remote.main"

    let library: ConnectionLibrary
    let options: LaunchOptions
    let activity = SessionActivity()
    let overview: OverviewWindowController
    private(set) var sessions: [SessionWindowController] = []
    private(set) var agentHost: AgentHost!
    private lazy var settings = SettingsWindowController(storeURL: library.store.fileURL, agentHost: agentHost)
    private var started = false
    private var pendingFiles: [URL] = []
    private var lastSessionNumber = 0
    /// The user has seen a window of this run (an agent launch then no longer quits by itself).
    private(set) var userInterfaceShown = false
    /// Quit without the "open sessions" question (SIGTERM, agent launches).
    var quitsWithoutAsking = false

    private static let logger = Logger(subsystem: "nrw.neuhaus.weitblick-remote", category: "windows")

    init(library: ConnectionLibrary, options: LaunchOptions) {
        self.library = library
        self.options = options
        overview = OverviewWindowController(library: library, activity: activity, options: options)
        super.init()
        overview.connect = { [weak self] ids in ids.forEach { self?.connect($0) } }
        overview.canClose = { [weak self] in self?.sessions.isEmpty ?? true }
        agentHost = AgentHost(coordinator: self, socketURL: options.agentSocketURL, logURL: options.agentLogURL)
    }

    /// Loads the connections, shows the overview and offers the Jump migration on the first start.
    /// Launched for an agent (`--agent-launch`) it shows nothing and only serves MCP.
    func start() async {
        await library.load()
        started = true
        agentHost.applyMasterSwitch()
        if options.agentLaunch {
            guard agentHost.isRunning || !pendingFiles.isEmpty else {
                // Agents are off (or another copy serves the socket): nothing to do.
                Self.logger.notice("agent launch without a running MCP server: quitting")
                quitsWithoutAsking = true
                NSApp.terminate(nil)
                return
            }
            if !pendingFiles.isEmpty { showOverview() }
        } else {
            showOverview()
        }
        if !options.agentLaunch, library.isFirstStart, library.loadError == nil, overview.jumpAvailable {
            overview.importFromJump()
        }
        if !pendingFiles.isEmpty {
            overview.importRDPFiles(pendingFiles)
            pendingFiles = []
        }
    }

    @objc func showOverview(_ sender: Any? = nil) {
        becomeRegularApp()
        overview.window?.tabGroup?.selectedWindow = overview.window
        overview.window?.makeKeyAndOrderFront(nil)
    }

    /// An agent launch runs as an accessory app (no Dock icon); a window for the user brings the
    /// Dock icon and the menu bar back.
    private func becomeRegularApp() {
        userInterfaceShown = true
        if NSApp.activationPolicy() != .regular { NSApp.setActivationPolicy(.regular) }
    }

    // MARK: Menu commands (File, Settings)

    @objc func newConnection(_ sender: Any?) {
        showOverview()
        overview.newConnection()
    }

    @objc func importFromJump(_ sender: Any?) {
        showOverview()
        overview.importFromJump()
    }

    @objc func importRDPFile(_ sender: Any?) {
        showOverview()
        overview.chooseRDPFiles()
    }

    @objc func showSettings(_ sender: Any?) {
        becomeRegularApp()
        settings.showWindow(nil)
    }

    @objc func showAbout(_ sender: Any?) {
        AboutPanel.show()
    }

    // MARK: Sessions

    /// Opens a session for the connection, or brings its running session to the front (an agent's
    /// windowless session gets its window: "Watch").
    func connect(_ id: UUID) {
        if let running = sessions.first(where: { $0.connectionID == id }) {
            if !running.wasPresented {
                present(running)
                running.publishAgentActivity()
            } else {
                running.activate()
            }
            return
        }
        guard let connection = library.connection(with: id) else { return }
        if connection.protocol == .vnc {
            openScreenSharing(connection)
            return
        }
        let controller = SessionWindowController(connection: connection, library: library, activity: activity,
                                                 sessionID: makeSessionID())
        track(controller)
        present(controller)
        controller.start()
    }

    /// Ids agents use for sessions: s1, s2, … for this run of the app.
    func makeSessionID() -> String {
        lastSessionNumber += 1
        return "s\(lastSessionNumber)"
    }

    /// Starts a windowless agent session (`sessionID` from `makeSessionID`).
    func openAgentSession(_ connection: Connection, sessionID: String, desktop: PixelSize) -> SessionWindowController {
        let controller = SessionWindowController(connection: connection, library: library, activity: activity,
                                                 sessionID: sessionID, agentDesktop: desktop)
        track(controller)
        controller.start()
        return controller
    }

    private func track(_ controller: SessionWindowController) {
        controller.onClose = { [weak self, weak controller] in
            guard let self, let controller else { return }
            sessions.removeAll { $0 === controller }
            agentHost.sessionClosed(controller)
        }
        sessions.append(controller)
    }

    /// Shows an agent session's tab without activating the app or switching the user's tab.
    func presentQuietly(_ controller: SessionWindowController) {
        guard let window = controller.window, !controller.wasPresented else { return }
        controller.wasPresented = true
        becomeRegularApp()
        if !AppSettings.sessionsInOwnWindows, let overviewWindow = overview.window,
           overviewWindow.isVisible || (overviewWindow.tabGroup?.windows.count ?? 0) > 1 {
            let group = overviewWindow.tabGroup
            let selected = group?.selectedWindow
            (group?.windows.last ?? overviewWindow).addTabbedWindow(window, ordered: .above)
            if let selected { group?.selectedWindow = selected }
        } else {
            window.orderFront(nil)
        }
        controller.publishAgentActivity()
    }

    private func openScreenSharing(_ connection: Connection) {
        guard let url = connection.vncURL else { return }
        NSWorkspace.shared.open(url)
        Task { await library.markConnected(connection.id) }
    }

    /// As the last tab of the overview's group, or as an own window (setting, or overview closed).
    private func present(_ controller: SessionWindowController) {
        guard let window = controller.window, let overviewWindow = overview.window else { return }
        controller.wasPresented = true
        becomeRegularApp()
        if AppSettings.sessionsInOwnWindows {
            // .disallowed only while showing it, so automatic tabbing can't pull it into a group;
            // "Merge All Windows" still works afterwards.
            window.tabbingMode = .disallowed
            window.makeKeyAndOrderFront(nil)
            window.tabbingMode = .automatic
        } else {
            if !overviewWindow.isVisible, overviewWindow.tabGroup?.windows.count ?? 1 <= 1 { showOverview() }
            let last = overviewWindow.tabGroup?.windows.last ?? overviewWindow
            last.addTabbedWindow(window, ordered: .above)
            window.makeKeyAndOrderFront(nil)
        }
    }

    /// Closes every session without asking (quitting).
    func closeAllSessions() {
        sessions.forEach { $0.close() }
    }

    func shouldTerminate() -> NSApplication.TerminateReply {
        let open = sessions.count
        guard open > 0, !quitsWithoutAsking else { return .terminateNow }
        let alert = NSAlert()
        alert.messageText = String(localized: "Quit \(AppInfo.name)?")
        alert.informativeText = open == 1
            ? String(localized: "The open session will be disconnected.")
            : String(localized: "The \(open) open sessions will be disconnected.")
        alert.addButton(withTitle: String(localized: "Quit"))
        alert.addButton(withTitle: String(localized: "Cancel"))
        return alert.runModal() == .alertFirstButtonReturn ? .terminateNow : .terminateCancel
    }

    // MARK: Files

    /// .rdp files from Finder ("Open With") or dropped on the Dock icon: imported, never connected.
    func open(_ urls: [URL]) {
        let files = urls.filter { $0.pathExtension.lowercased() == "rdp" }
        guard !files.isEmpty else { return }
        guard started else {
            pendingFiles += files
            return
        }
        showOverview()
        overview.importRDPFiles(files)
    }

    // MARK: Tabs (⌃⌥⌘1…9, ⌃⌥⌘← / →)

    /// Tab order: the overview's group as shown, then sessions in own windows in opening order.
    var orderedWindows: [NSWindow] {
        guard let overviewWindow = overview.window else { return [] }
        var windows = overviewWindow.tabGroup?.windows ?? []
        if windows.isEmpty, overviewWindow.isVisible { windows = [overviewWindow] }
        for case let window? in sessions.map(\.window) where !windows.contains(window) {
            windows.append(window)
        }
        return windows
    }

    /// Session windows in tab order (⌃⌥⌘2 is the first).
    private var sessionWindowsInOrder: [NSWindow] {
        orderedWindows.filter { $0 !== overview.window }
    }

    /// Tag 0 is the overview, tag n the n-th session.
    @objc func selectTab(_ sender: NSMenuItem) {
        guard sender.tag > 0 else { return showOverview() }
        let windows = sessionWindowsInOrder
        guard windows.indices.contains(sender.tag - 1) else { return }
        select(windows[sender.tag - 1])
    }

    @objc func selectNextTab(_ sender: Any?) { selectAdjacent(1) }
    @objc func selectPreviousTab(_ sender: Any?) { selectAdjacent(-1) }

    private func selectAdjacent(_ step: Int) {
        let windows = orderedWindows
        guard windows.count > 1 else { return }
        let current = windows.firstIndex { $0 === NSApp.keyWindow || $0 === NSApp.mainWindow } ?? 0
        select(windows[(current + step + windows.count) % windows.count])
    }

    private func select(_ window: NSWindow) {
        window.tabGroup?.selectedWindow = window
        window.makeKeyAndOrderFront(nil)
    }

    @objc func toggleSessionsInOwnWindows(_ sender: Any?) {
        AppSettings.sessionsInOwnWindows.toggle()
    }

    func validateMenuItem(_ item: NSMenuItem) -> Bool {
        switch item.action {
        case #selector(selectTab(_:)):
            return item.tag == 0 || sessionWindowsInOrder.indices.contains(item.tag - 1)
        case #selector(selectNextTab(_:)), #selector(selectPreviousTab(_:)):
            return orderedWindows.count > 1
        case #selector(toggleSessionsInOwnWindows(_:)):
            item.state = AppSettings.sessionsInOwnWindows ? .on : .off
            return true
        case #selector(importFromJump(_:)):
            return overview.jumpAvailable && library.loadError == nil
        case #selector(newConnection(_:)), #selector(importRDPFile(_:)):
            return library.loadError == nil
        default:
            return true
        }
    }

    /// Window menu: the tab entries are named after the current tab order each time it opens.
    func menuNeedsUpdate(_ menu: NSMenu) {
        let windows = sessionWindowsInOrder
        for item in menu.items where item.action == #selector(selectTab(_:)) && item.tag > 0 {
            let index = item.tag - 1
            item.isHidden = !windows.indices.contains(index)
            if !item.isHidden { item.title = windows[index].title }
        }
    }
}
