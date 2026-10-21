import Foundation

/// The MCP client as it introduced itself in `initialize` (`clientInfo`).
public struct MCPClientInfo: Equatable, Sendable {
    public var name: String
    public var version: String

    public init(name: String, version: String) {
        self.name = name
        self.version = version
    }

    public static let unknown = MCPClientInfo(name: "unknown client", version: "")

    /// "claude-code 2.1.3", for prompts and the audit log.
    public var displayName: String { version.isEmpty ? name : "\(name) \(version)" }
}

/// One content block of a tool result.
public enum ToolContent: Equatable, Sendable {
    case text(String)
    /// Base64 data plus MIME type (`image/png`, `image/jpeg`).
    case image(base64: String, mimeType: String)

    var json: JSONValue {
        switch self {
        case .text(let text): ["type": "text", "text": .string(text)]
        case .image(let data, let mimeType): ["type": "image", "data": .string(data), "mimeType": .string(mimeType)]
        }
    }
}

/// What a tool returns. Failures are results with `isError` (the model reads them), not JSON-RPC errors.
public struct ToolResult: Equatable, Sendable {
    public var content: [ToolContent]
    public var isError: Bool

    public init(content: [ToolContent], isError: Bool = false) {
        self.content = content
        self.isError = isError
    }

    public static func text(_ text: String) -> ToolResult { ToolResult(content: [.text(text)]) }
    public static func error(_ message: String) -> ToolResult { ToolResult(content: [.text(message)], isError: true) }

    var json: JSONValue {
        var object: [String: JSONValue] = ["content": .array(content.map(\.json))]
        if isError { object["isError"] = true }
        return .object(object)
    }
}

/// JSON-RPC 2.0 error codes used here.
public enum JSONRPCErrorCode {
    public static let parseError = -32700
    public static let invalidRequest = -32600
    public static let methodNotFound = -32601
    public static let invalidParams = -32602
}

/// The protocol side of one MCP connection (newline-delimited JSON-RPC 2.0, tools only), without
/// any I/O: feed it a line, it says what to send back or which tool to run. Tool calls run
/// elsewhere (they may wait); their answers go out through `response(to:result:)`.
public struct MCPServerCore: Sendable {
    /// Protocol revisions this server speaks, newest first.
    public static let supportedProtocolVersions = ["2025-06-18", "2025-03-26", "2024-11-05"]

    public enum Action: Equatable, Sendable {
        /// Send this line (a response or an error).
        case reply(String)
        /// Run the tool and send `response(to: id, result:)` when done.
        case callTool(id: JSONValue, name: String, arguments: JSONValue)
        /// Nothing to send (notifications, responses from the client).
        case none
    }

    public let serverName: String
    public let serverVersion: String
    public let instructions: String
    public let tools: [ToolDefinition]
    public private(set) var client = MCPClientInfo.unknown
    public private(set) var protocolVersion: String?
    public private(set) var isInitialized = false

    public init(serverName: String, serverVersion: String, instructions: String, tools: [ToolDefinition]) {
        self.serverName = serverName
        self.serverVersion = serverVersion
        self.instructions = instructions
        self.tools = tools
    }

    public mutating func handle(line: String) -> Action {
        let message: JSONValue
        do {
            message = try JSONValue.decode(line)
        } catch {
            return .reply(Self.error(id: .null, code: JSONRPCErrorCode.parseError, message: "Parse error"))
        }
        guard case .object(let object) = message else {
            // Batches were removed from MCP in 2025-06-18; older clients never send them for tools.
            return .reply(Self.error(id: .null, code: JSONRPCErrorCode.invalidRequest, message: "Invalid request"))
        }
        let id = object["id"]
        guard let method = object["method"]?.stringValue else {
            // A response from the client (we never ask it anything) or garbage.
            if id != nil, object["result"] != nil || object["error"] != nil { return .none }
            return .reply(Self.error(id: id ?? .null, code: JSONRPCErrorCode.invalidRequest, message: "Invalid request"))
        }
        let params = object["params"] ?? [:]
        guard let id, id != .null else { return handleNotification(method) }

        switch method {
        case "initialize":
            return .reply(Self.result(id: id, initialize(params)))
        case "ping":
            return .reply(Self.result(id: id, [:]))
        case "tools/list":
            return .reply(Self.result(id: id, ["tools": .array(tools.map(\.json))]))
        case "tools/call":
            guard let name = params["name"]?.stringValue else {
                return .reply(Self.error(id: id, code: JSONRPCErrorCode.invalidParams, message: "tools/call needs a tool name"))
            }
            guard tools.contains(where: { $0.name == name }) else {
                return .reply(Self.error(id: id, code: JSONRPCErrorCode.invalidParams, message: "Unknown tool: \(name)"))
            }
            let arguments = params["arguments"] ?? [:]
            guard arguments.objectValue != nil else {
                return .reply(Self.error(id: id, code: JSONRPCErrorCode.invalidParams, message: "Tool arguments must be an object"))
            }
            return .callTool(id: id, name: name, arguments: arguments)
        default:
            return .reply(Self.error(id: id, code: JSONRPCErrorCode.methodNotFound, message: "Method not found: \(method)"))
        }
    }

    private mutating func handleNotification(_ method: String) -> Action {
        if method == "notifications/initialized" { isInitialized = true }
        // notifications/cancelled and others: nothing to answer.
        return .none
    }

    private mutating func initialize(_ params: JSONValue) -> JSONValue {
        let requested = params["protocolVersion"]?.stringValue
        let version = Self.negotiate(requested)
        protocolVersion = version
        if let info = params["clientInfo"], let name = info["name"]?.stringValue, !name.isEmpty {
            client = MCPClientInfo(name: name, version: info["version"]?.stringValue ?? "")
        }
        return [
            "protocolVersion": .string(version),
            "capabilities": ["tools": ["listChanged": false]],
            "serverInfo": ["name": .string(serverName), "version": .string(serverVersion)],
            "instructions": .string(instructions),
        ]
    }

    /// The client's version if we speak it, else our newest (the client then decides whether it
    /// can continue).
    public static func negotiate(_ requested: String?) -> String {
        if let requested, supportedProtocolVersions.contains(requested) { return requested }
        return supportedProtocolVersions[0]
    }

    /// The answer line for a finished tool call.
    public static func response(to id: JSONValue, result: ToolResult) -> String {
        Self.result(id: id, result.json)
    }

    static func result(id: JSONValue, _ result: JSONValue) -> String {
        JSONValue.object(["jsonrpc": "2.0", "id": id, "result": result]).encoded()
    }

    static func error(id: JSONValue, code: Int, message: String) -> String {
        JSONValue.object(["jsonrpc": "2.0", "id": id, "error": ["code": .int(code), "message": .string(message)]]).encoded()
    }
}
