import SwiftUI

/// An AI agent's hold on a session.
struct AgentControl: Equatable {
    /// The MCP client, e.g. "claude-code 2.1.306".
    var client: String
    /// The user took over; the agent's tools wait for "Resume agent".
    var paused = false
}

/// The bar on top of a session an agent controls: who controls it, and the way back for the user.
struct AgentBanner: View {
    let control: AgentControl
    var takeOver: () -> Void
    var resume: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: control.paused ? "pause.circle.fill" : "sparkles")
                .foregroundStyle(control.paused ? Color.orange : Color.accentColor)
            if control.paused {
                Text("You have taken over from the agent (\(control.client))").lineLimit(1)
                Button("Resume agent", action: resume)
            } else {
                Text("An agent is controlling this session (\(control.client))").lineLimit(1)
                Button("Take over", action: takeOver).keyboardShortcut(.defaultAction)
            }
        }
        .font(.callout)
        .padding(.horizontal, 14)
        .padding(.vertical, 7)
        .background(.regularMaterial, in: Capsule())
        .overlay(Capsule().stroke(.separator))
        .shadow(radius: 6)
        .padding(8)
        .fixedSize()
    }
}
