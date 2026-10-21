import AgentControl
import Foundation
import os

/// Appends one line per agent tool call (and per permission event) to the audit log
/// (`~/Library/Logs/Weitblick Remote/agent.log`). Typed text appears by length only; results
/// (screens, clipboard contents) are never written.
final class AgentAuditLog: Sendable {
    let url: URL
    private let queue = DispatchQueue(label: "nrw.neuhaus.weitblick-remote.agent-log", qos: .utility)
    private static let logger = Logger(subsystem: "nrw.neuhaus.weitblick-remote", category: "agent")

    init(url: URL) {
        self.url = url
    }

    func record(_ entry: AuditEntry) {
        append(entry.line())
    }

    /// A free-form event: server start, prompts and their answers, sessions ending.
    func event(_ message: String) {
        append(AuditEntry.event(message, date: Date()))
    }

    private func append(_ line: String) {
        let url = url
        queue.async {
            let data = Data((line + "\n").utf8)
            do {
                if let handle = try? FileHandle(forWritingTo: url) {
                    defer { try? handle.close() }
                    try handle.seekToEnd()
                    try handle.write(contentsOf: data)
                } else {
                    try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
                    try data.write(to: url, options: .atomic)
                    try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
                }
            } catch {
                Self.logger.error("agent log not written: \(String(describing: error), privacy: .public)")
            }
        }
    }
}
