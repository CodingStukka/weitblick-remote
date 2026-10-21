import Foundation

/// A connection's "AI agent access" setting.
public enum AgentAccessPolicy: String, Sendable, CaseIterable {
    case ask, always, never
}

/// The user's answer in the permission prompt.
public enum PromptAnswer: String, Sendable, CaseIterable {
    /// This request only.
    case deny
    /// The session the request is for, until it disconnects.
    case allowOnce = "once"
    /// Every connect and tool call on the connection for `AccessGate.timedGrantDuration` (10 minutes).
    case allowTenMinutes = "ten-minutes"
    /// Every connect and tool call on the connection until local midnight.
    case allowToday = "today"
    /// Switches the connection to "Always allow" (persisted by the app).
    case alwaysAllow = "always"
}

/// What an agent wants: a session (new or existing) on a connection.
public struct AccessRequest: Equatable, Sendable {
    public var connectionID: String
    public var connectionName: String
    public var host: String
    /// MCP client display name ("claude-code 2.1.3").
    public var client: String
    /// The session the request is for; for `connect` the id the new session will get.
    public var sessionID: String

    public init(connectionID: String, connectionName: String, host: String, client: String, sessionID: String) {
        self.connectionID = connectionID
        self.connectionName = connectionName
        self.host = host
        self.client = client
        self.sessionID = sessionID
    }
}

/// An open permission prompt.
public struct AccessPrompt: Equatable, Identifiable, Sendable {
    public let id: Int
    public let request: AccessRequest
    public let opened: Date
}

/// A permission the user gave in a prompt (kept in memory only).
public struct AccessGrant: Equatable, Identifiable, Sendable {
    public enum Scope: Equatable, Sendable {
        /// "Allow once": this session until it disconnects.
        case session(String)
        /// "Allow for 10 minutes" / "Allow today": the whole connection until then.
        case until(Date)
    }

    public let id: Int
    public let connectionID: String
    public let connectionName: String
    public let client: String
    public let scope: Scope
    public let granted: Date
}

public enum AccessDecision: Equatable, Sendable {
    case allowed
    /// The message for the model.
    case refused(String)
}

/// The per-connection permission gate: policy, prompts and grants. Main actor; the clock, the
/// calendar (midnight) and the durations are injected so tests control time.
@MainActor
public final class AccessGate {
    public var now: () -> Date
    public var calendar: Calendar
    /// How long a tool call waits for an answer.
    public var promptTimeout: Duration
    /// "Allow for 10 minutes".
    public var timedGrantDuration: TimeInterval

    /// Shows a prompt. It may answer right away (`answer(_:with:)`), also from inside this call.
    public var showPrompt: (AccessPrompt) -> Void = { _ in }
    /// The prompt got answered or timed out: close its panel.
    public var closePrompt: (AccessPrompt) -> Void = { _ in }
    /// "Always allow": persist the policy for the connection id.
    public var persistAlwaysAllow: (String) -> Void = { _ in }
    /// Grants or prompts changed (settings list).
    public var onChange: () -> Void = {}

    private var grants: [AccessGrant] = []
    private var pending: [String: PendingPrompt] = [:]
    private var nextID = 1

    private enum Outcome {
        case answered(PromptAnswer, for: AccessRequest)
        case timedOut
        case cancelled(String)
    }

    private struct PendingPrompt {
        let prompt: AccessPrompt
        var waiters: [CheckedContinuation<Outcome, Never>] = []
        var timeout: Task<Void, Never>?
    }

    public init(
        now: @escaping () -> Date = Date.init, calendar: Calendar = .current,
        promptTimeout: Duration = .seconds(120), timedGrantDuration: TimeInterval = 600
    ) {
        self.now = now
        self.calendar = calendar
        self.promptTimeout = promptTimeout
        self.timedGrantDuration = timedGrantDuration
    }

    // MARK: - Checking

    /// Decides a request; for "Ask" without a grant it opens (or joins) the connection's prompt and
    /// waits for the answer. Call it for every connect and every tool call on a session.
    public func check(_ request: AccessRequest, policy: AgentAccessPolicy) async -> AccessDecision {
        while true {
            switch policy {
            case .never: return .refused(Self.neverMessage(request.connectionName))
            case .always: return .allowed
            case .ask: break
            }
            if isGranted(request) { return .allowed }
            switch await outcome(for: request) {
            case .answered(.deny, _):
                return .refused("The user denied access to \"\(request.connectionName)\".")
            case .answered(.allowOnce, let asked):
                // Granted for the session that was asked about; another session asks for itself.
                if asked.sessionID == request.sessionID { return .allowed }
            case .answered:
                return .allowed
            case .timedOut:
                return .refused(
                    "No answer from the user: the permission prompt for \"\(request.connectionName)\" was open for "
                        + "\(Self.describe(promptTimeout)). Ask the user to allow it and try again.")
            case .cancelled(let message):
                return .refused(message)
            }
        }
    }

    /// Whether a grant covers the request right now (no prompt). Expired grants are dropped.
    public func isGranted(_ request: AccessRequest) -> Bool {
        removeExpired()
        return grants.contains { grant in
            guard grant.connectionID == request.connectionID else { return false }
            switch grant.scope {
            case .session(let sessionID): return sessionID == request.sessionID
            case .until: return true
            }
        }
    }

    private func outcome(for request: AccessRequest) async -> Outcome {
        await withCheckedContinuation { continuation in
            if pending[request.connectionID] != nil {
                pending[request.connectionID]?.waiters.append(continuation)
                return
            }
            let prompt = AccessPrompt(id: nextID, request: request, opened: now())
            nextID += 1
            var entry = PendingPrompt(prompt: prompt, waiters: [continuation])
            let timeout = promptTimeout
            entry.timeout = Task { [weak self] in
                try? await Task.sleep(for: timeout)
                guard !Task.isCancelled else { return }
                self?.resolve(promptID: prompt.id, with: .timedOut)
            }
            pending[request.connectionID] = entry
            onChange()
            showPrompt(prompt)
        }
    }

    // MARK: - Answers

    /// The user's (or a test hook's) answer to an open prompt. Unknown ids are ignored.
    public func answer(_ promptID: Int, with answer: PromptAnswer) {
        guard let entry = pending.values.first(where: { $0.prompt.id == promptID }) else { return }
        let request = entry.prompt.request
        let date = now()
        switch answer {
        case .deny:
            break
        case .allowOnce:
            addGrant(for: request, scope: .session(request.sessionID), at: date)
        case .allowTenMinutes:
            addGrant(for: request, scope: .until(date.addingTimeInterval(timedGrantDuration)), at: date)
        case .allowToday:
            addGrant(for: request, scope: .until(nextMidnight(after: date)), at: date)
        case .alwaysAllow:
            persistAlwaysAllow(request.connectionID)
        }
        resolve(promptID: promptID, with: .answered(answer, for: request))
    }

    /// Refuses every open prompt (master switch turned off, app quitting).
    public func cancelAllPrompts(_ message: String) {
        for entry in pending.values {
            resolve(promptID: entry.prompt.id, with: .cancelled(message))
        }
    }

    public var openPrompts: [AccessPrompt] {
        pending.values.map(\.prompt).sorted { $0.id < $1.id }
    }

    private func resolve(promptID: Int, with outcome: Outcome) {
        guard let key = pending.first(where: { $0.value.prompt.id == promptID })?.key,
              let entry = pending.removeValue(forKey: key)
        else { return }
        entry.timeout?.cancel()
        closePrompt(entry.prompt)
        onChange()
        for waiter in entry.waiters {
            waiter.resume(returning: outcome)
        }
    }

    // MARK: - Grants

    /// Active grants, oldest first.
    public var activeGrants: [AccessGrant] {
        removeExpired()
        return grants
    }

    public func revoke(_ grantID: Int) {
        let count = grants.count
        grants.removeAll { $0.id == grantID }
        if grants.count != count { onChange() }
    }

    /// A session ended: its "Allow once" grant goes with it.
    public func sessionEnded(_ sessionID: String) {
        let count = grants.count
        grants.removeAll { $0.scope == .session(sessionID) }
        if grants.count != count { onChange() }
    }

    /// Forgets everything (master switch off).
    public func revokeAll() {
        guard !grants.isEmpty else { return }
        grants = []
        onChange()
    }

    /// The first local midnight after `date`.
    public func nextMidnight(after date: Date) -> Date {
        let startOfDay = calendar.startOfDay(for: date)
        return calendar.date(byAdding: .day, value: 1, to: startOfDay) ?? date.addingTimeInterval(86_400)
    }

    private func addGrant(for request: AccessRequest, scope: AccessGrant.Scope, at date: Date) {
        grants.append(AccessGrant(
            id: nextID, connectionID: request.connectionID, connectionName: request.connectionName,
            client: request.client, scope: scope, granted: date))
        nextID += 1
        onChange()
    }

    private func removeExpired() {
        let date = now()
        let count = grants.count
        grants.removeAll { grant in
            if case .until(let end) = grant.scope { return end <= date }
            return false
        }
        if grants.count != count { onChange() }
    }

    // MARK: - Messages

    public static func neverMessage(_ connectionName: String) -> String {
        "AI agent access is set to Never for \"\(connectionName)\". The user can change it in Weitblick Remote: "
            + "edit the connection, Security, AI agent access."
    }

    static func describe(_ duration: Duration) -> String {
        let seconds = Double(duration.components.seconds) + Double(duration.components.attoseconds) / 1e18
        if seconds >= 60, seconds.truncatingRemainder(dividingBy: 60) == 0 {
            let minutes = Int(seconds / 60)
            return minutes == 1 ? "1 minute" : "\(minutes) minutes"
        }
        return seconds == 1 ? "1 second" : "\(format(seconds)) seconds"
    }
}
