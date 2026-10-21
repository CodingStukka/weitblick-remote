import AgentControl
import AppKit
import ConnectionStore
import KeyboardEngine
import KeyboardEngineCarbon
import Observation
import os
import WeitblickKit

/// An MCP client on the socket (one `weitblick-mcp` process).
@MainActor
final class AgentClient {
    let id: Int
    let socket: AgentSocketConnection
    var core = MCPServerCore(serverName: "weitblick-remote", serverVersion: AppInfo.version,
                             instructions: ToolCatalog.instructions, tools: ToolCatalog.all)
    var name: String { core.client.displayName }

    init(socket: AgentSocketConnection) {
        self.id = socket.id
        self.socket = socket
    }
}

/// A session an agent works in: one it opened, or a session of the user it attached to.
@MainActor
final class AgentSessionRecord {
    let id: String
    let controller: SessionWindowController
    let clientID: Int
    /// Opened by the agent (closed with it); else the user's session (released, stays open).
    let owned: Bool
    var lastUsed = Date()
    /// Why the session is gone (the next tool call reports it).
    var ended: String?
    /// Where the agent last put the pointer (desktop pixels).
    var pointer: RemotePoint?
    /// For `clipboard_get` right after a copy: the input time and the clipboard changes before it.
    private(set) var lastInput: Date?
    private(set) var remoteChangesAtLastInput = 0
    private var keyboardEngine: KeyboardEngine?
    private var tail: Task<Void, Never>?

    init(id: String, controller: SessionWindowController, clientID: Int, owned: Bool) {
        self.id = id
        self.controller = controller
        self.clientID = clientID
        self.owned = owned
    }

    func noteInput() {
        lastInput = Date()
        remoteChangesAtLastInput = controller.clipboard?.remoteChangeCount ?? 0
    }

    /// The engine that resolves key names through the session's Windows keyboard layout.
    func keyboard(for session: RDPSession) -> KeyboardEngine {
        if let keyboardEngine { return keyboardEngine }
        let layoutID = WindowsKeyboardLayoutID(rawValue: session.configuration.keyboardLayout)
        let layout = UCKeyTranslateLayoutProvider(windowsLayout: layoutID)
            ?? UCKeyTranslateLayoutProvider(inputSourceID: "com.apple.keylayout.US")!
        let engine = KeyboardEngine(layout: layout)
        keyboardEngine = engine
        return engine
    }

    func store(_ engine: KeyboardEngine) {
        keyboardEngine = engine
    }

    /// Runs tool calls on this session one after another (parallel calls must not mix input).
    func serialized<T: Sendable>(_ body: @escaping @MainActor () async -> T) async -> T {
        let previous = tail
        let task = Task { @MainActor in
            await previous?.value
            return await body()
        }
        tail = Task { _ = await task.value }
        return await task.value
    }
}

/// Serves MCP for agents: the socket, its clients, the permission gate, the sessions agents use
/// and the audit log. Lives as long as the app; the master switch starts and stops the socket.
@MainActor @Observable
final class AgentHost {
    /// Agent sessions without a tool call for this long close (attached ones are released).
    static let idleLimit: TimeInterval = 30 * 60
    /// An agent launch quits after this long without clients and sessions.
    static let agentLaunchIdleQuit: TimeInterval = 60

    private(set) var isRunning = false
    private(set) var startError: String?
    /// Time grants for the settings list.
    private(set) var grants: [AccessGrant] = []

    @ObservationIgnored unowned let coordinator: WindowCoordinator
    @ObservationIgnored let socketURL: URL
    @ObservationIgnored let log: AgentAuditLog
    @ObservationIgnored let gate = AccessGate()
    /// DEBUG: answers for the next prompts instead of showing the panel (`--debug-agent-prompt`).
    @ObservationIgnored var scriptedAnswers: [PromptAnswer]?
    @ObservationIgnored private var server: AgentSocketServer?
    @ObservationIgnored private(set) var clients: [Int: AgentClient] = [:]
    @ObservationIgnored private(set) var records: [String: AgentSessionRecord] = [:]
    @ObservationIgnored private let panels = AgentPromptPanels()
    @ObservationIgnored private var housekeeping: Timer?
    @ObservationIgnored private var idleSince = Date()
    /// "Always allow" answers until the store has them.
    @ObservationIgnored private var alwaysAllowPending: Set<UUID> = []

    static let logger = Logger(subsystem: "nrw.neuhaus.weitblick-remote", category: "agent")

    init(coordinator: WindowCoordinator, socketURL: URL, logURL: URL) {
        self.coordinator = coordinator
        self.socketURL = socketURL
        self.log = AgentAuditLog(url: logURL)
        gate.showPrompt = { [weak self] in self?.promptOpened($0) }
        gate.closePrompt = { [weak self] in self?.panels.close($0) }
        gate.persistAlwaysAllow = { [weak self] in self?.persistAlwaysAllow($0) }
        gate.onChange = { [weak self] in self?.refreshGrants() }
        panels.onAnswer = { [weak self] id, answer in
            self?.log.event("prompt \(id) answered: \(answer.rawValue)")
            self?.gate.answer(id, with: answer)
        }
    }

    // MARK: Master switch

    /// Starts or stops the socket to match Settings → "AI agents (MCP)".
    func applyMasterSwitch() {
        if AppSettings.agentControlEnabled { start() } else { stop() }
    }

    private func start() {
        guard server == nil else { return }
        let server = AgentSocketServer(path: socketURL.path) { [weak self] in self?.accept($0) }
        do {
            try server.start()
        } catch {
            startError = error.description
            Self.logger.error("MCP socket not started: \(error.description, privacy: .public)")
            return
        }
        self.server = server
        startError = nil
        isRunning = true
        idleSince = Date()
        log.event("MCP server started on \(socketURL.path)")
        let timer = Timer(timeInterval: 15, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.tidyUp() }
        }
        RunLoop.main.add(timer, forMode: .common)
        housekeeping = timer
    }

    /// Quitting: the socket file goes away with the app.
    func shutDown() {
        stop()
    }

    private func stop() {
        housekeeping?.invalidate()
        housekeeping = nil
        startError = nil
        guard let server else { return }
        self.server = nil
        server.stop()
        isRunning = false
        for client in clients.values {
            client.socket.close()
            clientGone(client)
        }
        gate.cancelAllPrompts("AI agents were turned off in Weitblick Remote.")
        gate.revokeAll()
        log.event("MCP server stopped")
    }

    // MARK: Clients

    private func accept(_ socket: AgentSocketConnection) {
        guard server != nil else { return socket.close() }
        let client = AgentClient(socket: socket)
        clients[client.id] = client
        socket.start(
            onLine: { [weak self, weak client] line in
                guard let self, let client else { return }
                self.handle(line, from: client)
            },
            onClose: { [weak self, weak client] in
                guard let self, let client else { return }
                self.clientGone(client)
            })
    }

    private func handle(_ line: String, from client: AgentClient) {
        let wasInitialized = client.core.protocolVersion != nil
        let action = client.core.handle(line: line)
        if !wasInitialized, let version = client.core.protocolVersion {
            log.event("client \(client.id) connected: \"\(client.name)\" (MCP \(version))")
        }
        switch action {
        case .reply(let reply):
            client.socket.send(reply)
        case .callTool(let id, let name, let arguments):
            Task {
                let result = await self.call(name, arguments, from: client)
                client.socket.send(MCPServerCore.response(to: id, result: result))
            }
        case .none:
            break
        }
    }

    private func clientGone(_ client: AgentClient) {
        guard clients.removeValue(forKey: client.id) != nil else { return }
        for record in records.values where record.clientID == client.id {
            release(record, because: "its client disconnected")
        }
        idleSince = Date()
        log.event("client \(client.id) disconnected: \"\(client.name)\"")
    }

    // MARK: Sessions

    /// Ends the agent's use of a session: its own session closes (unless the user took it over
    /// and watches it), a user's session is released.
    func release(_ record: AgentSessionRecord, because reason: String) {
        records[record.id] = nil
        gate.sessionEnded(record.id)
        guard record.ended == nil else { return }
        let controller = record.controller
        if record.owned, !(controller.agentControl?.paused == true && controller.isWindowVisible) {
            log.event("session \(record.id) closed: \(reason)")
            controller.close()
        } else {
            log.event("session \(record.id) released: \(reason)")
            controller.detachAgent()
        }
    }

    func register(_ record: AgentSessionRecord) {
        records[record.id] = record
    }

    func unregister(_ id: String) {
        records[id] = nil
        gate.sessionEnded(id)
    }

    /// A session window closed (by the user, the server or the agent).
    func sessionClosed(_ controller: SessionWindowController) {
        guard let record = records[controller.agentSessionID], record.controller === controller else { return }
        record.ended = controller.agentEndMessage ?? "The session was closed."
        gate.sessionEnded(record.id)
    }

    /// Every 15 s: idle sessions, and quitting an agent launch nobody needs.
    private func tidyUp() {
        let now = Date()
        for record in records.values where record.ended == nil && now.timeIntervalSince(record.lastUsed) > Self.idleLimit {
            release(record, because: "30 minutes without a tool call")
        }
        if !clients.isEmpty || !coordinator.sessions.isEmpty { idleSince = now }
        if coordinator.options.agentLaunch, !coordinator.userInterfaceShown,
           now.timeIntervalSince(idleSince) > Self.agentLaunchIdleQuit {
            log.event("no agent needs the app any more: quitting")
            coordinator.quitsWithoutAsking = true
            NSApp.terminate(nil)
        }
    }

    // MARK: Permission

    /// The connection's agent access as the gate sees it.
    func policy(for connection: Connection) -> AgentAccessPolicy {
        if alwaysAllowPending.contains(connection.id) { return .always }
        return AgentAccessPolicy(rawValue: connection.security.agentAccess.rawValue) ?? .ask
    }

    func request(for connection: Connection, client: AgentClient, sessionID: String) -> AccessRequest {
        AccessRequest(connectionID: connection.id.uuidString, connectionName: connection.name, host: connection.host,
                      client: client.name, sessionID: sessionID)
    }

    private func promptOpened(_ prompt: AccessPrompt) {
        log.event("prompt \(prompt.id) opened: client=\"\(prompt.request.client)\" connection=\"\(prompt.request.connectionName) (\(prompt.request.host))\"")
        if var answers = scriptedAnswers {
            // Tests: answer from the launch arguments; none left = nobody answers (timeout).
            guard !answers.isEmpty else { return }
            let answer = answers.removeFirst()
            scriptedAnswers = answers
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { [weak self] in
                MainActor.assumeIsolated {
                    self?.log.event("prompt \(prompt.id) answered: \(answer.rawValue) (scripted)")
                    self?.gate.answer(prompt.id, with: answer)
                }
            }
            return
        }
        panels.show(prompt)
    }

    private func persistAlwaysAllow(_ connectionID: String) {
        guard let id = UUID(uuidString: connectionID) else { return }
        alwaysAllowPending.insert(id)
        Task {
            do {
                try await coordinator.library.modify(id) { $0.security.agentAccess = .always }
            } catch {
                Self.logger.error("saving Always allow failed: \(String(describing: error), privacy: .public)")
            }
            alwaysAllowPending.remove(id)
        }
    }

    private func refreshGrants() {
        grants = gate.activeGrants
    }

    /// Settings → Revoke.
    func revoke(_ grant: AccessGrant) {
        gate.revoke(grant.id)
        log.event("grant for \"\(grant.connectionName)\" revoked by the user")
        refreshGrants()
    }

    /// Grants expire silently; the settings list asks before drawing.
    func currentGrants() -> [AccessGrant] {
        gate.activeGrants
    }
}
