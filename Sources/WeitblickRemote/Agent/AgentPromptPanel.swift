import AgentControl
import AppKit
import SwiftUI

/// The permission prompt: a floating panel that never activates the app or takes keyboard focus,
/// so it can appear while the app runs hidden and the user works elsewhere. One per open prompt.
@MainActor
final class AgentPromptPanels {
    /// The user's answer for a prompt id.
    var onAnswer: (Int, PromptAnswer) -> Void = { _, _ in }
    private var panels: [Int: NSPanel] = [:]

    func show(_ prompt: AccessPrompt) {
        guard panels[prompt.id] == nil else { return }
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 420, height: 200),
                            styleMask: [.titled, .nonactivatingPanel, .utilityWindow], backing: .buffered, defer: false)
        panel.title = String(localized: "AI agent access")
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = true
        panel.isReleasedWhenClosed = false
        panel.isRestorable = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        let hosting = NSHostingView(rootView: AgentPromptView(prompt: prompt) { [weak self] answer in
            self?.onAnswer(prompt.id, answer)
        })
        panel.contentView = hosting
        panel.setContentSize(hosting.fittingSize)
        place(panel)
        panels[prompt.id] = panel
        panel.orderFrontRegardless()
        NSSound(named: "Glass")?.play()
    }

    func close(_ prompt: AccessPrompt) {
        panels.removeValue(forKey: prompt.id)?.close()
    }

    /// Top right of the screen with the menu bar, below earlier prompts.
    private func place(_ panel: NSPanel) {
        guard let screen = NSScreen.screens.first ?? NSScreen.main else { return }
        let visible = screen.visibleFrame
        let size = panel.frame.size
        let below = panels.values.map(\.frame.minY).min() ?? visible.maxY
        let top = max(visible.minY + size.height, min(visible.maxY, below) - 12)
        panel.setFrameOrigin(NSPoint(x: visible.maxX - size.width - 16, y: top - size.height))
    }
}

private struct AgentPromptView: View {
    let prompt: AccessPrompt
    let answer: (PromptAnswer) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: "sparkles").font(.system(size: 26)).foregroundStyle(Color.accentColor)
                VStack(alignment: .leading, spacing: 4) {
                    Text("“\(prompt.request.client)” wants to control “\(prompt.request.connectionName)”")
                        .font(.headline)
                        .fixedSize(horizontal: false, vertical: true)
                    Text("\(prompt.request.host): screenshots, mouse, keyboard and clipboard of this Windows session. The agent waits up to 2 minutes for your answer.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            HStack {
                Button("Deny") { answer(.deny) }
                Spacer()
                Button("Allow once") { answer(.allowOnce) }
                    .help("This session, until it disconnects")
            }
            HStack {
                Button("Allow for 10 minutes") { answer(.allowTenMinutes) }
                Button("Allow today") { answer(.allowToday) }
                    .help("Until midnight")
                Spacer()
                Button("Always allow") { answer(.alwaysAllow) }
                    .help("Sets this connection’s agent access to Always allow")
            }
        }
        .padding(16)
        .frame(width: 440)
    }
}
