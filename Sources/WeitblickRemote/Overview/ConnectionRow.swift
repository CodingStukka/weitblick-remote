import ConnectionStore
import SwiftUI

/// One connection in the overview list.
struct ConnectionRow: View {
    let connection: Connection
    let status: SessionStatus?
    /// An AI agent working in the connection's session.
    var agent: AgentActivity?
    /// Opens the agent's session window ("Watch").
    var watch: () -> Void = {}

    var body: some View {
        HStack(spacing: 12) {
            icon
            VStack(alignment: .leading, spacing: 2) {
                Text(connection.name).font(.headline).lineLimit(1)
                Text(address).font(.subheadline).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
            }
            Spacer(minLength: 12)
            if let agent { agentBadge(agent) }
            VStack(alignment: .trailing, spacing: 2) {
                if let status {
                    Text(status.label).font(.caption.weight(.medium)).foregroundStyle(status.tint)
                }
                Text(lastUsed).font(.caption).foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 5)
        .contentShape(Rectangle())
    }

    private func agentBadge(_ agent: AgentActivity) -> some View {
        HStack(spacing: 6) {
            Label(agent.paused ? String(localized: "Agent paused") : String(localized: "Agent"), systemImage: "sparkles")
                .font(.caption.weight(.medium))
                .foregroundStyle(agent.paused ? Color.orange : Color.accentColor)
                .help(String(localized: "Controlled by “\(agent.client)”"))
            if agent.windowless {
                Button("Watch", action: watch).controlSize(.small)
            }
        }
    }

    private var icon: some View {
        Image(systemName: connection.protocol == .vnc ? "macwindow" : "pc")
            .font(.system(size: 19, weight: .regular))
            .foregroundStyle(connection.protocol == .vnc ? Color.purple : Color.accentColor)
            .frame(width: 38, height: 38)
            .background(.quaternary.opacity(0.6), in: RoundedRectangle(cornerRadius: 9))
            .overlay(alignment: .bottomTrailing) {
                if let status {
                    Circle().fill(status.tint).frame(width: 10, height: 10)
                        .overlay(Circle().stroke(Color(nsColor: .windowBackgroundColor), lineWidth: 2))
                        .offset(x: 3, y: 3)
                }
            }
    }

    /// `DOMAIN\user @ host:port · Screen Sharing`, leaving out what is empty or standard.
    private var address: String {
        var user = connection.username
        if !connection.domain.isEmpty, !user.isEmpty { user = "\(connection.domain)\\\(user)" }
        let standardPort = connection.protocol == .vnc ? 5900 : 3389
        var host = connection.host
        if connection.port != standardPort { host += ":\(connection.port)" }
        var text = user.isEmpty ? host : "\(user) @ \(host)"
        if connection.protocol == .vnc { text += " · " + String(localized: "Screen Sharing") }
        return text
    }

    private var lastUsed: String {
        guard let date = connection.lastConnected else { return String(localized: "Never connected") }
        let now = Date()
        guard now.timeIntervalSince(date) >= 60 else { return String(localized: "Last used just now") }
        return String(localized: "Last used \(Self.relativeFormatter.localizedString(for: date, relativeTo: now))")
    }

    /// In the UI's language ("2 hours ago" / "vor 2 Stunden"), whatever the region setting.
    private static let relativeFormatter: RelativeDateTimeFormatter = {
        let formatter = RelativeDateTimeFormatter()
        formatter.locale = Locale(identifier: Bundle.main.preferredLocalizations.first ?? "en")
        formatter.unitsStyle = .full
        return formatter
    }()
}

extension SessionStatus {
    var tint: Color {
        switch self {
        case .connecting: .secondary
        case .connected: .green
        case .reconnecting: .orange
        case .disconnected: .red
        }
    }
}
