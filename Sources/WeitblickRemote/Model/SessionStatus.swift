import Foundation
import Observation

/// What a session tab shows about its connection.
enum SessionStatus: Equatable, Sendable {
    case connecting
    case connected
    /// The network dropped; attempt `attempt` to get the same Windows session back runs.
    case reconnecting(attempt: Int)
    case disconnected

    var label: String {
        switch self {
        case .connecting: String(localized: "Connecting…")
        case .connected: String(localized: "Connected")
        case .reconnecting(let attempt): String(localized: "Reconnecting (attempt \(attempt))…")
        case .disconnected: String(localized: "Disconnected")
        }
    }
}

/// An AI agent working in a connection's session (overview badge and "Watch").
struct AgentActivity: Equatable, Sendable {
    /// The MCP client, e.g. "claude-code 2.1.306".
    var client: String
    /// The user took over; the agent waits.
    var paused: Bool
    /// The session has no window yet ("Watch" opens one).
    var windowless: Bool
}

/// Status of every open session by connection, for the overview list.
@MainActor @Observable
final class SessionActivity {
    private(set) var statuses: [UUID: SessionStatus] = [:]
    private(set) var agents: [UUID: AgentActivity] = [:]

    func set(_ status: SessionStatus?, for connectionID: UUID) {
        statuses[connectionID] = status
        if status == nil { agents[connectionID] = nil }
    }

    func setAgent(_ agent: AgentActivity?, for connectionID: UUID) {
        agents[connectionID] = agent
    }
}
