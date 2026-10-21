import Foundation

/// One line of the agent audit log (`~/Library/Logs/Weitblick Remote/agent.log`). Typed and pasted
/// text is logged by length only.
public struct AuditEntry: Sendable {
    public var date: Date
    public var client: String
    /// "Name (host)", or nil for tools without a connection.
    public var connection: String?
    public var tool: String
    public var arguments: JSONValue
    /// nil = success, else the error message.
    public var error: String?
    public var duration: Duration

    public init(
        date: Date, client: String, connection: String?, tool: String, arguments: JSONValue, error: String?,
        duration: Duration
    ) {
        self.date = date
        self.client = client
        self.connection = connection
        self.tool = tool
        self.arguments = arguments
        self.error = error
        self.duration = duration
    }

    /// Argument keys whose values are replaced by their length.
    static let redactedKeys: Set<String> = ["text"]

    public func line(timeZone: TimeZone = .current) -> String {
        var parts = [
            Self.timestamp(date, timeZone: timeZone),
            "client=" + quoted(client),
        ]
        if let connection { parts.append("connection=" + quoted(connection)) }
        parts.append("tool=\(tool)")
        parts.append("args=" + Self.redacted(arguments).encoded())
        let milliseconds = duration.components.seconds * 1000 + duration.components.attoseconds / 1_000_000_000_000_000
        parts.append(error.map { "error=" + quoted($0) } ?? "ok")
        parts.append("\(milliseconds)ms")
        return parts.joined(separator: " ")
    }

    /// A free-form event (server start, prompt answers).
    public static func event(_ message: String, date: Date, timeZone: TimeZone = .current) -> String {
        timestamp(date, timeZone: timeZone) + " " + message
    }

    static func redacted(_ arguments: JSONValue) -> JSONValue {
        guard case .object(var object) = arguments else { return arguments }
        for key in redactedKeys {
            if let text = object[key]?.stringValue {
                object[key] = .string("<\(text.count) characters>")
            }
        }
        return .object(object)
    }

    private static func timestamp(_ date: Date, timeZone: TimeZone) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        formatter.timeZone = timeZone
        return formatter.string(from: date)
    }

    private func quoted(_ text: String) -> String {
        JSONValue.string(text).encoded()
    }
}
