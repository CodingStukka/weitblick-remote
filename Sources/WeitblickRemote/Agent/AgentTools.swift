import AgentControl
import AppKit
import ConnectionStore
import KeyboardEngine
import WeitblickKit

/// A tool failed for a reason the model should read (not an argument problem).
struct ToolFailure: Error {
    let message: String
    init(_ message: String) { self.message = message }
}

/// Numbers for the model: whole numbers without a fraction, else rounded to `digits`.
private func format(_ value: Double, digits: Int = 1) -> String {
    if value.rounded() == value, abs(value) < 1e9 { return String(Int(value)) }
    let factor = pow(10, Double(digits))
    return String((value * factor).rounded() / factor)
}

/// What a tool call touched, for the audit log.
@MainActor
private final class ToolCall {
    var connection: String?
}

/// The MCP tools (see `ToolCatalog` for their schemas).
extension AgentHost {
    /// Pauses between input events, at the pace of a fast typist: Windows apps read modifier state
    /// asynchronously, and the WinUI Notepad mixes up Unicode characters that arrive faster (at 8 ms
    /// per character "ÄÖÜ" arrived as "ÜÜÜ"; the keyboard e2e found the same for Shift at 4 ms).
    private static let keyPace: Duration = .milliseconds(20)
    private static let chordPace: Duration = .milliseconds(50)
    private static let characterPace: Duration = .milliseconds(25)
    private static let connectTimeout: Duration = .seconds(90)
    /// About two minutes of typing; longer texts go through the clipboard.
    private static let maxTypedCharacters = 5_000

    /// Runs one tool call and writes the audit line.
    func call(_ name: String, _ arguments: JSONValue, from client: AgentClient) async -> ToolResult {
        let started = Date()
        let clock = ContinuousClock.now
        let call = ToolCall()
        let result: ToolResult
        do {
            result = try await run(name, ToolArguments(arguments), client: client, call: call)
        } catch let error as ToolArgumentError {
            result = .error(error.message)
        } catch let error as ToolFailure {
            result = .error(error.message)
        } catch {
            result = .error(String(describing: error))
        }
        let errorText: String? = result.isError ? result.content.compactMap {
            if case .text(let text) = $0 { text } else { nil }
        }.first : nil
        log.record(AuditEntry(date: started, client: client.name, connection: call.connection, tool: name,
                              arguments: arguments, error: errorText, duration: clock.duration(to: .now)))
        return result
    }

    private func run(_ name: String, _ arguments: ToolArguments, client: AgentClient, call: ToolCall) async throws -> ToolResult {
        switch name {
        case "list_connections": return listConnections(client)
        case "list_sessions": return listSessions(client)
        case "connect": return try await connect(arguments, client: client, call: call)
        case "disconnect": return try disconnect(arguments, client: client, call: call)
        default: break
        }
        let record = try await session(arguments, client: client, call: call)
        return try await record.serialized { [self] in
            do {
                return try await runOnSession(name, arguments, record)
            } catch let error as ToolArgumentError {
                return .error(error.message)
            } catch let error as ToolFailure {
                return .error(error.message)
            } catch {
                return .error(String(describing: error))
            }
        }
    }

    private func runOnSession(_ name: String, _ arguments: ToolArguments, _ record: AgentSessionRecord) async throws -> ToolResult {
        let rdp = try connectedSession(record)
        let mapping = ScreenMapping(desktopWidth: rdp.desktopSize.width, desktopHeight: rdp.desktopSize.height)
        let result: ToolResult
        switch name {
        case "screenshot":
            return try screenshot(record, rdp, mapping, region: arguments.optionalRect("region"))
        case "click": result = try await click(arguments, record, rdp, mapping)
        case "mouse_move":
            let point = try remotePoint(arguments, mapping)
            rdp.sendMouseMove(to: point)
            record.pointer = point
            record.noteInput()
            result = .text("Moved the pointer to \(describe(point, mapping)).")
        case "mouse_down", "mouse_up": result = try await press(arguments, down: name == "mouse_down", record, rdp, mapping)
        case "drag": result = try await drag(arguments, record, rdp, mapping)
        case "scroll": result = try await scroll(arguments, record, rdp, mapping)
        case "type_text": result = try await typeText(arguments, record, rdp)
        case "key": result = try await key(arguments, record, rdp)
        case "clipboard_get": return try await clipboardGet(record)
        case "clipboard_set": return try await clipboardSet(arguments, record)
        case "wait":
            let seconds = try arguments.number("seconds", in: 0...30)
            try? await Task.sleep(for: .seconds(seconds))
            return .text("Waited \(format(seconds)) s.")
        case "wait_for_screen":
            let timeout = try arguments.number("timeout", in: 0...60, default: 10)
            let settle = try arguments.integer("settle_ms", in: 100...5000, default: 500)
            return .text(await waitForScreen(record, timeout: timeout, settle: Double(settle) / 1000))
        case "read_screen": return try await readScreen(arguments, rdp, mapping)
        case "find_text": return try await findText(arguments, rdp, mapping)
        default:
            throw ToolFailure("Unknown tool '\(name)'.")
        }
        guard try arguments.bool("screenshot", default: false) else { return result }
        let settled = await waitForScreen(record, timeout: 5, settle: 0.5)
        let current = try connectedSession(record)
        let shot = try screenshot(record, current, ScreenMapping(desktopWidth: current.desktopSize.width,
                                                                  desktopHeight: current.desktopSize.height), region: nil)
        return ToolResult(content: result.content + [.text(settled)] + shot.content)
    }

    // MARK: Connections and sessions

    private func listConnections(_ client: AgentClient) -> ToolResult {
        let connections = coordinator.library.connections.filter { $0.protocol == .rdp }
            .sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
        let items: [JSONValue] = connections.map { connection in
            var item: [String: JSONValue] = [
                "name": .string(connection.name), "id": .string(connection.id.uuidString),
                "host": .string(hostAndPort(connection)), "agent_access": .string(policy(for: connection).rawValue),
            ]
            if let controller = coordinator.sessions.first(where: { $0.connectionID == connection.id }) {
                item["open_session"] = .string(controller.agentSessionID)
                item["session_owner"] = .string(owner(of: controller, for: client))
            }
            return .object(item)
        }
        guard !items.isEmpty else { return .text("There are no saved RDP connections.") }
        return .text("""
            Saved RDP connections. agent_access: ask = the user is asked to allow it, always = allowed, \
            never = refused.
            \(JSONValue.array(items).encoded())
            """)
    }

    private func listSessions(_ client: AgentClient) -> ToolResult {
        var items: [JSONValue] = []
        for controller in coordinator.sessions where controller.currentConnection.protocol == .rdp {
            let record = records[controller.agentSessionID]
            var item: [String: JSONValue] = [
                "session": .string(controller.agentSessionID), "connection": .string(controller.currentConnection.name),
                "host": .string(hostAndPort(controller.currentConnection)), "owner": .string(owner(of: controller, for: client)),
                "status": .string(statusName(controller.status)),
            ]
            if let rdp = controller.connectedSession {
                let mapping = ScreenMapping(desktopWidth: rdp.desktopSize.width, desktopHeight: rdp.desktopSize.height)
                item["screenshot_size"] = .string("\(mapping.screenshotWidth)×\(mapping.screenshotHeight)")
                item["scale"] = .double(mapping.scale)
            }
            if record?.clientID == client.id, controller.agentControl?.paused == true { item["paused_by_user"] = true }
            items.append(.object(item))
        }
        for record in records.values where record.clientID == client.id && record.ended != nil {
            items.append(["session": .string(record.id), "status": "ended", "reason": .string(record.ended ?? "")])
        }
        guard !items.isEmpty else { return .text("No sessions are open. Use connect.") }
        return .text("""
            Open sessions. owner: you = yours, user = the user's own session (using its id asks the user and \
            lets you work in it), other agent = not available.
            \(JSONValue.array(items).encoded())
            """)
    }

    private func connect(_ arguments: ToolArguments, client: AgentClient, call: ToolCall) async throws -> ToolResult {
        let connection = try connection(named: arguments.nonEmptyString("connection"))
        call.connection = label(connection)
        guard connection.protocol == .rdp else {
            throw ToolFailure("“\(connection.name)” is a Screen Sharing (VNC) connection; agents can only use RDP connections.")
        }
        let width = try arguments.integer("width", in: 800...1920, default: 1280) & ~1
        let height = try arguments.integer("height", in: 600...1200, default: 800)
        let show = try arguments.bool("show", default: false)

        if let running = coordinator.sessions.first(where: { $0.connectionID == connection.id }) {
            if let record = records[running.agentSessionID], record.controller === running, record.ended == nil {
                guard record.clientID == client.id else {
                    throw ToolFailure("Another agent is working in “\(connection.name)” right now.")
                }
                if show { coordinator.presentQuietly(running) }
                return .text("Already connected: " + describe(record))
            }
            // The user has it open: a second logon would throw the user out, so work in theirs.
            let record = try await attach(running, client: client)
            if show { coordinator.presentQuietly(running) }
            return .text("Attached to the user's open session (the user sees what you do): " + describe(record))
        }

        if !connection.security.disableNLA,
           connection.username.isEmpty || coordinator.library.password(for: connection.id) == nil {
            throw ToolFailure("No user name or password is saved for “\(connection.name)”. Ask the user to connect once by hand in Weitblick Remote and save the password.")
        }
        let sessionID = coordinator.makeSessionID()
        if case .refused(let message) = await gate.check(request(for: connection, client: client, sessionID: sessionID),
                                                          policy: policy(for: connection)) {
            throw ToolFailure(message)
        }
        guard clients[client.id] != nil else { throw ToolFailure("The client disconnected.") }
        if coordinator.sessions.contains(where: { $0.connectionID == connection.id }) {
            throw ToolFailure("“\(connection.name)” was opened while waiting for permission. Call connect again.")
        }
        let latest = coordinator.library.connection(with: connection.id) ?? connection
        let controller = coordinator.openAgentSession(latest, sessionID: sessionID,
                                                      desktop: PixelSize(width: width, height: height))
        let record = AgentSessionRecord(id: sessionID, controller: controller, clientID: client.id, owned: true)
        register(record)
        controller.attachAgent(client: client.name)
        if show { coordinator.presentQuietly(controller) }
        if let failure = await controller.waitUntilConnected(timeout: Self.connectTimeout) {
            unregister(sessionID)
            if !controller.isWindowVisible { controller.close() }
            throw ToolFailure(failure)
        }
        let ready = await waitForDesktop(record)
        return .text("Connected: " + describe(record)
            + (ready ? " Take a screenshot to see it." : " The screen is still blank (Windows may still be signing in): wait_for_screen, then take a screenshot."))
    }

    /// After the logon Windows paints a flat white, black or blue screen for a while: wait until
    /// there is a picture and it has settled, so the first screenshot shows the desktop.
    private func waitForDesktop(_ record: AgentSessionRecord) async -> Bool {
        let deadline = Date().addingTimeInterval(30)
        while Date() < deadline {
            guard let rdp = record.controller.connectedSession else { return false }
            if !ScreenCapture.looksBlank(rdp) {
                _ = await ScreenSettler(settle: 1, timeout: 10).wait(for: { record.controller.connectedSession })
                return true
            }
            try? await Task.sleep(for: .milliseconds(250))
        }
        return false
    }

    private func disconnect(_ arguments: ToolArguments, client: AgentClient, call: ToolCall) throws -> ToolResult {
        let id = try arguments.string("session")
        guard let record = records[id], record.clientID == client.id else {
            throw ToolFailure("Session '\(id)' is not one of yours (list_sessions shows them).")
        }
        call.connection = label(record.controller.currentConnection)
        if record.ended != nil {
            unregister(id)
            return .text("Session \(id) had already ended.")
        }
        let keptByUser = !record.owned || (record.controller.agentControl?.paused == true && record.controller.isWindowVisible)
        release(record, because: "the agent disconnected")
        return .text(keptByUser ? "Released session \(id); it stays open for the user." : "Disconnected session \(id).")
    }

    /// The session a tool names, checked against the gate on every call.
    private func session(_ arguments: ToolArguments, client: AgentClient, call: ToolCall) async throws -> AgentSessionRecord {
        let id = try arguments.string("session")
        if let record = records[id] {
            guard record.clientID == client.id else { throw ToolFailure("Another agent is working in session \(id).") }
            let connection = coordinator.library.connection(with: record.controller.connectionID) ?? record.controller.currentConnection
            call.connection = label(connection)
            if let ended = record.ended { throw ToolFailure("Session \(id) has ended: \(ended) Connect again.") }
            if case .refused(let message) = await gate.check(request(for: connection, client: client, sessionID: id),
                                                              policy: policy(for: connection)) {
                throw ToolFailure(message)
            }
            guard records[id] === record, record.ended == nil else { throw ToolFailure("Session \(id) has ended.") }
            if record.controller.agentControl?.paused == true {
                throw ToolFailure("Paused by the user: they took over session \(id). Wait and try again later; the user can click “Resume agent”.")
            }
            record.lastUsed = Date()
            return record
        }
        guard let controller = coordinator.sessions.first(where: { $0.agentSessionID == id }) else {
            throw ToolFailure("Unknown session '\(id)'. list_sessions shows the open sessions.")
        }
        call.connection = label(controller.currentConnection)
        return try await attach(controller, client: client)
    }

    /// Takes control of a session the user has open, after asking.
    private func attach(_ controller: SessionWindowController, client: AgentClient) async throws -> AgentSessionRecord {
        let connection = coordinator.library.connection(with: controller.connectionID) ?? controller.currentConnection
        guard controller.currentConnection.protocol == .rdp else { throw ToolFailure("Agents can only use RDP sessions.") }
        guard controller.agentControl == nil else { throw ToolFailure("Another agent is working in “\(connection.name)”.") }
        guard controller.status == .connected else {
            throw ToolFailure("The user's session on “\(connection.name)” is not connected (\(statusName(controller.status))).")
        }
        if case .refused(let message) = await gate.check(
            request(for: connection, client: client, sessionID: controller.agentSessionID), policy: policy(for: connection)) {
            throw ToolFailure(message)
        }
        guard clients[client.id] != nil else { throw ToolFailure("The client disconnected.") }
        guard coordinator.sessions.contains(where: { $0 === controller }), controller.agentControl == nil else {
            throw ToolFailure("The session on “\(connection.name)” changed while waiting for permission. Try again.")
        }
        let record = AgentSessionRecord(id: controller.agentSessionID, controller: controller, clientID: client.id, owned: false)
        register(record)
        controller.attachAgent(client: client.name)
        log.event("session \(record.id) attached: the user's session on \"\(connection.name)\"")
        return record
    }

    private func connectedSession(_ record: AgentSessionRecord) throws -> RDPSession {
        if let ended = record.ended { throw ToolFailure("Session \(record.id) has ended: \(ended)") }
        guard let rdp = record.controller.connectedSession else {
            throw ToolFailure("Session \(record.id) is \(statusName(record.controller.status)) right now. Wait a moment and try again.")
        }
        return rdp
    }

    private func connection(named query: String) throws -> Connection {
        let connections = coordinator.library.connections
        if let id = UUID(uuidString: query), let match = connections.first(where: { $0.id == id }) { return match }
        let matches = connections.filter { $0.name.compare(query, options: [.caseInsensitive, .diacriticInsensitive]) == .orderedSame }
        if matches.count == 1 { return matches[0] }
        if matches.count > 1 {
            throw ToolFailure("Several connections are named “\(query)”; use the id from list_connections.")
        }
        throw ToolFailure("No connection named “\(query)”. list_connections shows the saved connections.")
    }

    private func owner(of controller: SessionWindowController, for client: AgentClient) -> String {
        if let record = records[controller.agentSessionID], record.controller === controller {
            return record.clientID == client.id ? "you" : "other agent"
        }
        return controller.agentControl == nil ? "user" : "other agent"
    }

    private func describe(_ record: AgentSessionRecord) -> String {
        let connection = record.controller.currentConnection
        var text = "session \(record.id) on “\(connection.name)” (\(connection.host))."
        if let rdp = record.controller.connectedSession {
            let mapping = ScreenMapping(desktopWidth: rdp.desktopSize.width, desktopHeight: rdp.desktopSize.height)
            text += " Screenshots are \(mapping.summary)."
        }
        return text
    }

    private func label(_ connection: Connection) -> String {
        "\(connection.name) (\(connection.host))"
    }

    private func hostAndPort(_ connection: Connection) -> String {
        connection.port == 3389 ? connection.host : "\(connection.host):\(connection.port)"
    }

    /// English for the model, whatever language the app shows.
    private func statusName(_ status: SessionStatus) -> String {
        switch status {
        case .connecting: "connecting"
        case .connected: "connected"
        case .reconnecting(let attempt): "reconnecting (attempt \(attempt))"
        case .disconnected: "disconnected"
        }
    }

    // MARK: Screen

    private func screenshot(_ record: AgentSessionRecord, _ rdp: RDPSession, _ mapping: ScreenMapping,
                            region: PixelRect?) throws -> ToolResult {
        let desktopRect = try region.map { try mapping.desktopRect($0) }
        guard let pixels = ScreenCapture.copy(rdp, rect: desktopRect) else {
            throw ToolFailure("Session \(record.id) has no picture yet. Wait a moment and try again.")
        }
        let width = max(1, Int((Double(pixels.rect.width) * mapping.scale).rounded()))
        let height = max(1, Int((Double(pixels.rect.height) * mapping.scale).rounded()))
        guard let image = ScreenCapture.resized(pixels.image, width: width, height: height),
              let encoded = ScreenCapture.encode(image) else {
            throw ToolFailure("The screenshot couldn’t be encoded.")
        }
        var text: String
        if region != nil {
            let origin = mapping.screenshotPoint(desktopX: Double(pixels.rect.x), desktopY: Double(pixels.rect.y))
            text = "Region of the screen: \(width)×\(height) px whose top left is (\(Int(origin.x.rounded())), "
                + "\(Int(origin.y.rounded()))) on the \(mapping.screenshotWidth)×\(mapping.screenshotHeight) screen "
                + "(scale \(format(mapping.scale, digits: 4))). For clicks add the region's top left to the image pixel."
        } else {
            text = "Screenshot \(mapping.summary)."
        }
        if let pointer = record.pointer {
            text += " Pointer at \(describe(pointer, mapping))."
        }
        return ToolResult(content: [.image(base64: encoded.data.base64EncodedString(), mimeType: encoded.mimeType), .text(text)])
    }

    private func waitForScreen(_ record: AgentSessionRecord, timeout: Double, settle: Double) async -> String {
        let settler = ScreenSettler(settle: settle, timeout: timeout)
        switch await settler.wait(for: { record.controller.connectedSession }) {
        case .settled(let after):
            return "The screen is quiet (settled after \(format(after)) s)."
        case .busy(let change):
            var text = "The screen was still changing after \(format(timeout)) s"
            if let change, let rdp = record.controller.connectedSession {
                let mapping = ScreenMapping(desktopWidth: rdp.desktopSize.width, desktopHeight: rdp.desktopSize.height)
                let box = mapping.screenshotRect(PixelRect(x: Double(change.x), y: Double(change.y),
                                                           width: Double(change.width), height: Double(change.height)))
                text += " (last change around \(describe(box)))"
            }
            return text + "."
        case .sessionGone:
            return "The session is not connected any more."
        }
    }

    private func readScreen(_ arguments: ToolArguments, _ rdp: RDPSession, _ mapping: ScreenMapping) async throws -> ToolResult {
        let lines = try await recognize(arguments, rdp, mapping)
        guard !lines.isEmpty else { return .text("No text recognized.") }
        let rows = lines.map { line in
            let box = screenshotBox(line.box, mapping)
            return "“\(line.text)” \(describe(box)) click (\(Int(box.center.x.rounded())), \(Int(box.center.y.rounded())))"
        }
        return .text("\(lines.count) lines of text (screenshot pixels: box x, y, width×height; click = box center):\n"
            + rows.joined(separator: "\n"))
    }

    private func findText(_ arguments: ToolArguments, _ rdp: RDPSession, _ mapping: ScreenMapping) async throws -> ToolResult {
        let query = try arguments.nonEmptyString("query")
        let lines = try await recognize(arguments, rdp, mapping)
        let matches = TextMatching.find(query, in: lines.map(\.text))
        guard !matches.isEmpty else {
            let seen = lines.prefix(40).map { "“\($0.text)”" }.joined(separator: ", ")
            return .text("No match for “\(query)”." + (seen.isEmpty ? " No text recognized." : " Text on screen: \(seen)"))
        }
        let rows = matches.enumerated().map { index, match in
            let line = lines[match.lineIndex]
            let box = screenshotBox(line.boxOfRange(match.range) ?? line.box, mapping)
            var row = "\(index + 1). “\(line.text[match.range])”"
            if String(line.text[match.range]) != line.text { row += " in “\(line.text)”" }
            row += " \(describe(box)) click (\(Int(box.center.x.rounded())), \(Int(box.center.y.rounded())))"
            if match.errors > 0 { row += ", approximate (\(match.errors) differences)" }
            return row
        }
        return .text("\(matches.count) matches for “\(query)” (screenshot pixels, best first):\n" + rows.joined(separator: "\n"))
    }

    private func recognize(_ arguments: ToolArguments, _ rdp: RDPSession, _ mapping: ScreenMapping) async throws -> [ScreenReader.Line] {
        let region = try arguments.optionalRect("region").map { try mapping.desktopRect($0) }
        guard let pixels = ScreenCapture.copy(rdp, rect: region) else { throw ToolFailure("The session has no picture yet.") }
        do {
            return try await ScreenReader.read(pixels)
        } catch {
            throw ToolFailure("Text recognition failed: \(error.localizedDescription)")
        }
    }

    private func screenshotBox(_ desktop: CGRect, _ mapping: ScreenMapping) -> PixelRect {
        mapping.screenshotRect(PixelRect(x: desktop.minX, y: desktop.minY, width: desktop.width, height: desktop.height))
    }

    private func describe(_ box: PixelRect) -> String {
        "(\(Int(box.x.rounded())), \(Int(box.y.rounded())), \(Int(box.width.rounded()))×\(Int(box.height.rounded())))"
    }

    private func describe(_ point: RemotePoint, _ mapping: ScreenMapping) -> String {
        let shown = mapping.screenshotPoint(desktopX: Double(point.x), desktopY: Double(point.y))
        return "(\(Int(shown.x.rounded())), \(Int(shown.y.rounded())))"
    }

    // MARK: Mouse

    private func remotePoint(_ arguments: ToolArguments, _ mapping: ScreenMapping) throws -> RemotePoint {
        let point = try mapping.desktopPoint(PixelPoint(x: arguments.number("x"), y: arguments.number("y")))
        return RemotePoint(x: point.x, y: point.y)
    }

    private func mouseButton(_ arguments: ToolArguments) throws -> MouseButton {
        switch try arguments.choice("button", of: ["left", "right", "middle"], default: "left") {
        case "right": .right
        case "middle": .middle
        default: .left
        }
    }

    private func click(_ arguments: ToolArguments, _ record: AgentSessionRecord, _ rdp: RDPSession,
                       _ mapping: ScreenMapping) async throws -> ToolResult {
        let point = try remotePoint(arguments, mapping)
        let button = try mouseButton(arguments)
        let count = try arguments.integer("count", in: 1...3, default: 1)
        let modifiers = try HeldModifiers.parse(arguments.has("modifiers") ? arguments.stringArray("modifiers") : [])
        await send(HeldModifiers.press(modifiers), to: rdp)
        rdp.sendMouseMove(to: point)
        try? await Task.sleep(for: .milliseconds(20))
        for _ in 0..<count {
            rdp.sendMouseButton(button, down: true, at: point)
            try? await Task.sleep(for: .milliseconds(30))
            rdp.sendMouseButton(button, down: false, at: point)
            try? await Task.sleep(for: .milliseconds(50))
        }
        await send(HeldModifiers.release(modifiers), to: rdp)
        record.pointer = point
        record.noteInput()
        let what = count == 1 ? "Clicked" : count == 2 ? "Double-clicked" : "Triple-clicked"
        return .text("\(what) at \(describe(point, mapping)).")
    }

    private func press(_ arguments: ToolArguments, down: Bool, _ record: AgentSessionRecord, _ rdp: RDPSession,
                       _ mapping: ScreenMapping) async throws -> ToolResult {
        let button = try mouseButton(arguments)
        var point = record.pointer ?? RemotePoint(x: rdp.desktopSize.width / 2, y: rdp.desktopSize.height / 2)
        if arguments.has("x") || arguments.has("y") {
            point = try remotePoint(arguments, mapping)
            rdp.sendMouseMove(to: point)
            try? await Task.sleep(for: .milliseconds(20))
        }
        rdp.sendMouseButton(button, down: down, at: point)
        record.pointer = point
        record.noteInput()
        return .text("\(down ? "Pressed" : "Released") the mouse button at \(describe(point, mapping)).")
    }

    private func drag(_ arguments: ToolArguments, _ record: AgentSessionRecord, _ rdp: RDPSession,
                      _ mapping: ScreenMapping) async throws -> ToolResult {
        let path = try arguments.points("path")
        guard path.count >= 2 else { throw ToolArgumentError("'path' needs at least two points.") }
        let points = try path.map { point -> RemotePoint in
            let mapped = try mapping.desktopPoint(point)
            return RemotePoint(x: mapped.x, y: mapped.y)
        }
        let button = try mouseButton(arguments)
        rdp.sendMouseMove(to: points[0])
        try? await Task.sleep(for: .milliseconds(30))
        rdp.sendMouseButton(button, down: true, at: points[0])
        try? await Task.sleep(for: .milliseconds(60))
        for (from, to) in zip(points, points.dropFirst()) {
            let distance = hypot(Double(to.x - from.x), Double(to.y - from.y))
            let steps = max(1, Int(distance / 16))
            for step in 1...steps {
                let t = Double(step) / Double(steps)
                rdp.sendMouseMove(to: RemotePoint(x: from.x + Int((Double(to.x - from.x) * t).rounded()),
                                                  y: from.y + Int((Double(to.y - from.y) * t).rounded())))
                try? await Task.sleep(for: .milliseconds(10))
            }
        }
        try? await Task.sleep(for: .milliseconds(60))
        rdp.sendMouseButton(button, down: false, at: points[points.count - 1])
        record.pointer = points[points.count - 1]
        record.noteInput()
        return .text("Dragged from \(describe(points[0], mapping)) to \(describe(points[points.count - 1], mapping)).")
    }

    private func scroll(_ arguments: ToolArguments, _ record: AgentSessionRecord, _ rdp: RDPSession,
                        _ mapping: ScreenMapping) async throws -> ToolResult {
        let point = try remotePoint(arguments, mapping)
        let dx = try arguments.integer("dx", in: -50...50, default: 0)
        let dy = try arguments.integer("dy", in: -50...50, default: 0)
        guard dx != 0 || dy != 0 else { throw ToolArgumentError("Give dx or dy (wheel notches).") }
        rdp.sendMouseMove(to: point)
        try? await Task.sleep(for: .milliseconds(20))
        // RDP: positive vertical scrolls up; the tool's dy > 0 scrolls down.
        for _ in 0..<abs(dy) {
            rdp.sendMouseWheel(vertical: dy > 0 ? -120 : 120, horizontal: 0, at: point)
            try? await Task.sleep(for: .milliseconds(15))
        }
        for _ in 0..<abs(dx) {
            rdp.sendMouseWheel(vertical: 0, horizontal: dx > 0 ? 120 : -120, at: point)
            try? await Task.sleep(for: .milliseconds(15))
        }
        record.pointer = point
        record.noteInput()
        return .text("Scrolled \(dy != 0 ? "\(abs(dy)) notches \(dy > 0 ? "down" : "up")" : "")"
            + "\(dx != 0 && dy != 0 ? " and " : "")\(dx != 0 ? "\(abs(dx)) notches \(dx > 0 ? "right" : "left")" : "")"
            + " at \(describe(point, mapping)).")
    }

    // MARK: Keyboard

    private func typeText(_ arguments: ToolArguments, _ record: AgentSessionRecord, _ rdp: RDPSession) async throws -> ToolResult {
        let text = try arguments.string("text")
        guard text.count <= Self.maxTypedCharacters else {
            throw ToolArgumentError("'text' is longer than \(Self.maxTypedCharacters) characters; use clipboard_set and ctrl+v.")
        }
        for group in TextTyping.actions(for: text) {
            rdp.send(group)
            try? await Task.sleep(for: Self.characterPace)
        }
        record.noteInput()
        return .text("Typed \(text.count) characters.")
    }

    private func key(_ arguments: ToolArguments, _ record: AgentSessionRecord, _ rdp: RDPSession) async throws -> ToolResult {
        let keys = try arguments.string("keys")
        let chords = try KeyCombo.parse(keys)
        let count = try arguments.integer("repeat", in: 1...100, default: 1)
        var engine = record.keyboard(for: rdp)
        // Refuse unknown keys before anything is pressed.
        var probe = engine
        _ = try KeyCombo.actions(for: chords, engine: &probe)
        for _ in 0..<count {
            for chord in chords {
                await send(try KeyCombo.actions(for: [chord], engine: &engine), to: rdp)
                try? await Task.sleep(for: Self.chordPace)
            }
        }
        record.store(engine)
        record.noteInput()
        return .text("Pressed \(keys)\(count > 1 ? " \(count) times" : "").")
    }

    private func send(_ actions: [RDPKeyAction], to rdp: RDPSession) async {
        for action in actions {
            rdp.send([action])
            try? await Task.sleep(for: Self.keyPace)
        }
    }

    // MARK: Clipboard

    private func clipboard(_ record: AgentSessionRecord) throws -> ClipboardSync {
        guard record.controller.currentConnection.redirection.clipboard, let clipboard = record.controller.clipboard,
              clipboard.isChannelReady else {
            throw ToolFailure("The clipboard isn't available in this session: clipboard redirection is off for “\(record.controller.currentConnection.name)” (the user can turn it on in the connection's Redirection settings).")
        }
        return clipboard
    }

    private func clipboardGet(_ record: AgentSessionRecord) async throws -> ToolResult {
        let clipboard = try clipboard(record)
        // A copy (ctrl+c) right before arrives as a clipboard change shortly after the keys.
        if let lastInput = record.lastInput, clipboard.remoteChangeCount == record.remoteChangesAtLastInput {
            let deadline = lastInput.addingTimeInterval(1.5)
            while Date() < deadline, clipboard.remoteChangeCount == record.remoteChangesAtLastInput {
                try? await Task.sleep(for: .milliseconds(50))
            }
        }
        guard let text = await clipboard.remoteText() else { return .text("(The remote clipboard holds no text.)") }
        return .text(text.isEmpty ? "(The remote clipboard text is empty.)" : text)
    }

    private func clipboardSet(_ arguments: ToolArguments, _ record: AgentSessionRecord) async throws -> ToolResult {
        let text = try arguments.string("text")
        let clipboard = try clipboard(record)
        guard await clipboard.offerText(text) else {
            throw ToolFailure("The session did not take the clipboard text. Try again.")
        }
        return .text("The remote clipboard now holds \(text.count) characters. Paste with key ctrl+v.")
    }
}
