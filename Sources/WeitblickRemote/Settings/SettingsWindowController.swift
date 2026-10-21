import AgentControl
import AppKit
import SwiftUI

/// "Settings…": the app-wide switches.
@MainActor
final class SettingsWindowController: NSWindowController {
    init(storeURL: URL, agentHost: AgentHost) {
        let hosting = NSHostingController(rootView: SettingsView(storeURL: storeURL, agentHost: agentHost))
        hosting.sizingOptions = .preferredContentSize
        let window = NSWindow(contentViewController: hosting)
        window.title = String(localized: "Settings")
        window.styleMask = [.titled, .closable]
        window.tabbingMode = .disallowed
        window.isReleasedWhenClosed = false
        window.center()
        super.init(window: window)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }
}

private struct SettingsView: View {
    let storeURL: URL
    let agentHost: AgentHost
    @AppStorage(AppSettings.sessionsInOwnWindowsKey) private var ownWindows = false
    @AppStorage(AppSettings.confirmCloseSessionKey) private var confirmClose = true
    @AppStorage(SystemShortcutCapture.defaultsKey) private var capture = SystemShortcutCapture.fullScreen.rawValue

    var body: some View {
        Form {
            Section("Sessions") {
                Toggle("Open sessions in separate windows", isOn: $ownWindows)
                Toggle("Ask before closing a session", isOn: $confirmClose)
            }
            Section {
                Picker("Send system shortcuts to Windows", selection: $capture) {
                    Text("In full screen").tag(SystemShortcutCapture.fullScreen.rawValue)
                    Text("Always").tag(SystemShortcutCapture.always.rawValue)
                    Text("Never").tag(SystemShortcutCapture.never.rawValue)
                }
                .onChange(of: capture) {
                    NotificationCenter.default.post(name: .systemShortcutCaptureChanged, object: nil)
                }
            } footer: {
                Text("⌘⇥, ⌘Space and ⌃←/→ then go to Windows instead of macOS (needs Accessibility access).")
                    .font(.caption).foregroundStyle(.secondary)
            }
            AgentSettingsSection(host: agentHost)
            Section("Data") {
                LabeledContent("Connections") {
                    Button("Show in Finder") { NSWorkspace.shared.activateFileViewerSelecting([storeURL]) }
                }
                Text(storeURL.path).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            }
        }
        .formStyle(.grouped)
        .frame(width: 460)
        .fixedSize(horizontal: false, vertical: true)
    }
}

/// Settings → "AI agents (MCP)": the master switch, the setup line and the time grants.
private struct AgentSettingsSection: View {
    let host: AgentHost
    @AppStorage(AppSettings.agentControlEnabledKey) private var enabled = false

    /// The `claude mcp add` line for this copy of the app.
    private var setupCommand: String {
        let helper = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/weitblick-mcp").path
        return "claude mcp add weitblick -- \"\(helper)\""
    }

    var body: some View {
        Section {
            Toggle("Let AI agents control Windows sessions", isOn: $enabled)
                .onChange(of: enabled) { host.applyMasterSwitch() }
            if enabled {
                if let error = host.startError {
                    Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.red).font(.callout)
                }
                LabeledContent("Set up Claude Code") {
                    Button("Copy") {
                        // The user's own click: the general pasteboard is what they asked for.
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(setupCommand, forType: .string)
                    }
                }
                Text(setupCommand).font(.caption.monospaced()).foregroundStyle(.secondary).textSelection(.enabled)
                grants
            }
        } header: {
            Text("AI agents (MCP)")
        } footer: {
            Text("Agents connect through the weitblick-mcp helper. Each connection decides in its Security settings whether agents are asked about, always allowed or never allowed. Everything agents do is logged in ~/Library/Logs/Weitblick Remote/agent.log.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    /// Time-limited permissions; they expire on their own, so the list redraws every few seconds.
    private var grants: some View {
        TimelineView(.periodic(from: .now, by: 5)) { _ in
            let current = host.grants.isEmpty ? [] : host.currentGrants()
            if current.isEmpty {
                LabeledContent("Permissions", value: String(localized: "None"))
            } else {
                ForEach(current, id: \.id) { grant in
                    LabeledContent {
                        Button("Revoke") { host.revoke(grant) }
                    } label: {
                        Text("“\(grant.client)” on “\(grant.connectionName)”")
                        Text(scopeText(grant.scope)).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        }
    }

    private func scopeText(_ scope: AccessGrant.Scope) -> String {
        switch scope {
        case .session(let id): String(localized: "This session (\(id))")
        case .until(let date): String(localized: "Until \(date.formatted(date: .omitted, time: .shortened))")
        }
    }
}
