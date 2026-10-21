#if DEBUG
import AgentControl
import AppKit
import ConnectionStore
import WeitblickKit
import SwiftUI

/// Development-only launch arguments for screenshots and VM tests without touching the keyboard,
/// the mouse or the Keychain (compiled out of Release):
///
///     --debug-testvm-credentials   in-memory passwords; the .testvm.env password for its host
///     --debug-testvm-wrong-password  …but a wrong one, to reach the sign-in sheet
///     --debug-connect <name>       opens that connection (only the test VM network 10.211.55.x; repeatable)
///     --debug-certificate <answer>@<s>  answers an open certificate sheet (once | always | reject)
///     --debug-signin <s>           answers an open sign-in sheet with the .testvm.env credentials (saved)
///     --debug-show <what>          editor:<name>[:<page>] | new | import | signin:<name>
///                                  | certificate[-changed]:<name> | delete:<name> | settings | about
///     --debug-import-confirm <s>   applies the open Jump import after s seconds (to reach the report)
///     --debug-select-tab <n>@<s>   selects tab n (0 = overview, 1… sessions) after s seconds (repeatable)
///     --debug-dark                 dark appearance
///     --debug-agent-prompt <list>  answers the agent permission prompts in order (deny, once,
///                                  ten-minutes, today, always; "none" = nobody answers)
///     --debug-agent-grant-seconds <s>   "Allow for 10 minutes" lasts s seconds instead
///     --debug-agent-prompt-timeout <s>  the prompt waits s seconds instead of 2 minutes
@MainActor
struct DebugHooks {
    let coordinator: WindowCoordinator
    private var arguments: [String] { coordinator.options.arguments }

    private static var testCredentials: InMemoryCredentialStore?

    static func credentialStore(_ options: LaunchOptions) -> (any CredentialStore)? {
        guard options.arguments.contains("--debug-testvm-credentials") else { return nil }
        let store = InMemoryCredentialStore()
        testCredentials = store
        return store
    }

    /// Agent test settings; before `start()` so the first MCP client already sees them.
    func configureAgentHost() {
        let host = coordinator.agentHost!
        if let list = value("--debug-agent-prompt") {
            host.scriptedAnswers = list.split(separator: ",").compactMap { PromptAnswer(rawValue: String($0)) }
        }
        if let seconds = value("--debug-agent-grant-seconds").flatMap(Double.init) {
            host.gate.timedGrantDuration = seconds
        }
        if let seconds = value("--debug-agent-prompt-timeout").flatMap(Double.init) {
            host.gate.promptTimeout = .seconds(seconds)
        }
    }

    func run() {
        if arguments.contains("--debug-dark") { NSApp.appearance = NSAppearance(named: .darkAqua) }
        seedTestCredentials()
        for name in values("--debug-connect") { connect(name) }
        if let what = value("--debug-show") { show(what) }
        for answer in values("--debug-certificate") {
            let parts = answer.split(separator: "@")
            guard parts.count == 2, let seconds = Double(parts[1]) else { continue }
            let decision: CertificateDecision = switch parts[0] {
            case "always": .acceptPermanently
            case "once": .acceptOnce
            default: .reject
            }
            after(seconds) { answerCertificate(decision) }
        }
        if let seconds = value("--debug-signin").flatMap(Double.init) {
            after(seconds) { signIn() }
        }
        if let seconds = value("--debug-import-confirm").flatMap(Double.init) {
            after(seconds) { confirmImport() }
        }
        for value in values("--debug-select-tab") {
            let parts = value.split(separator: "@")
            guard parts.count == 2, let index = Int(parts[0]), let seconds = Double(parts[1]) else { continue }
            after(seconds) { selectTab(index) }
        }
    }

    private func value(_ flag: String) -> String? { LaunchOptions.value(of: flag, in: arguments) }

    private func values(_ flag: String) -> [String] {
        zip(arguments, arguments.dropFirst()).filter { $0.0 == flag }.map(\.1)
    }

    private func after(_ seconds: Double, _ action: @escaping @MainActor () -> Void) {
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { MainActor.assumeIsolated(action) }
    }

    private func connection(named name: String) -> Connection? {
        coordinator.library.connections.first { $0.name == name }
    }

    private static var testVM: TestVMEnvironment? {
        TestVMEnvironment.locate().flatMap { try? TestVMEnvironment(contentsOf: $0) }
    }

    private func seedTestCredentials() {
        guard let store = Self.testCredentials, let vm = Self.testVM else { return }
        let password = arguments.contains("--debug-testvm-wrong-password") ? "wrong-\(UUID().uuidString.prefix(6))" : vm.password
        for connection in coordinator.library.connections where connection.host == vm.host {
            try? store.set(password, for: connection.id)
        }
    }

    /// The first attached sheet of a session window showing `View`.
    private func sessionSheet<View: SwiftUI.View>(_ type: View.Type) -> View? {
        coordinator.sessions.lazy.compactMap { ($0.window?.attachedSheet?.contentViewController as? NSHostingController<View>)?.rootView }.first
    }

    private func answerCertificate(_ decision: CertificateDecision) {
        guard let sheet = sessionSheet(CertificateView.self) else { return NSLog("debug-certificate: no sheet") }
        sheet.onDecision(decision)
    }

    private func signIn() {
        guard let sheet = sessionSheet(SignInView.self), let vm = Self.testVM else { return NSLog("debug-signin: no sheet") }
        sheet.onConnect(Credentials(username: vm.username, domain: "", password: vm.password), true)
    }

    /// Hard rule: development sessions go to the test VM network only, never to imported hosts.
    private func connect(_ name: String) {
        guard let connection = connection(named: name) else { return NSLog("debug-connect: no connection \(name)") }
        guard connection.host.hasPrefix("10.211.55.") else { return NSLog("debug-connect: refused, not the test VM network") }
        coordinator.connect(connection.id)
    }

    private func show(_ what: String) {
        let parts = what.split(separator: ":").map(String.init)
        let target = parts.count > 1 ? connection(named: parts[1]) : nil
        let overview = coordinator.overview
        switch parts[0] {
        case "new": overview.newConnection()
        case "import": overview.importFromJump()
        case "settings": coordinator.showSettings(nil)
        case "about": coordinator.showAbout(nil)
        case "editor":
            guard let target, let window = overview.window else { return }
            let page = parts.count > 2 ? ConnectionEditorView.Page(rawValue: parts[2]) ?? .general : .general
            window.presentSheet(ConnectionEditorView(
                draft: ConnectionDraft(connection: target, isNew: false, hasStoredPassword: true), page: page,
                onSave: { _ in }, onCancel: {}))
        case "delete": if let target { overview.confirmDelete([target.id]) }
        case "signin", "certificate", "certificate-changed":
            guard let target else { return }
            showSessionSheet(parts[0], for: target)
        default: NSLog("debug-show: unknown \(what)")
        }
    }

    /// The session sheets need a session window; this one never connects (sheets only).
    private func showSessionSheet(_ kind: String, for connection: Connection) {
        guard let window = coordinator.overview.window else { return }
        if kind == "signin" {
            window.presentSheet(SignInView(connectionName: connection.name, message: DisconnectReason.logonFailed.message,
                                           username: connection.username, domain: connection.domain,
                                           onConnect: { _, _ in }, onCancel: {}))
            return
        }
        let certificate = CertificateDetails(
            host: connection.host, port: UInt16(clamping: connection.port), commonName: "WIN11-TEST",
            subject: "CN = WIN11-TEST", issuer: "CN = WIN11-TEST",
            fingerprint: "3A:9F:12:C4:58:0B:7E:21:96:DD:4F:A0:6C:13:E8:B5:72:09:4D:3E:AF:61:C7:28:5B:90:1E:F4:83:6A:D2:0C",
            changed: kind == "certificate-changed", hostnameMismatch: false)
        window.presentSheet(CertificateView(certificate: certificate, connectionName: connection.name) { _ in })
    }

    private func confirmImport() {
        guard let sheet = coordinator.overview.window?.attachedSheet,
              let hosting = sheet.contentViewController as? NSHostingController<JumpImportView> else { return }
        Task { await hosting.rootView.flow.apply() }
    }

    private func selectTab(_ index: Int) {
        let item = NSMenuItem()
        item.tag = index
        coordinator.selectTab(item)
    }
}
#endif
