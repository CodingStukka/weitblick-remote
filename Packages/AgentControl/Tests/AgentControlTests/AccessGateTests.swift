@testable import AgentControl
import Foundation
import Testing

/// The permission gate with an injected clock and calendar; prompts are answered by the test.
@MainActor
struct AccessGateTests {
    @MainActor
    final class Harness {
        var date = Date(timeIntervalSince1970: 1_792_000_000) // 2026-10-14 17:46:40 UTC = 19:46:40 Berlin
        var shown: [AccessPrompt] = []
        var closed: [Int] = []
        var persisted: [String] = []
        /// Answers for the next prompts, in order; nil = leave it open.
        var answers: [PromptAnswer?] = []
        let gate: AccessGate

        init(timeout: Duration = .seconds(120)) {
            var calendar = Calendar(identifier: .gregorian)
            calendar.timeZone = TimeZone(identifier: "Europe/Berlin")!
            gate = AccessGate(now: { Date() }, calendar: calendar, promptTimeout: timeout)
            gate.now = { [unowned self] in date }
            gate.showPrompt = { [unowned self] prompt in
                shown.append(prompt)
                if !answers.isEmpty, let answer = answers.removeFirst() { gate.answer(prompt.id, with: answer) }
            }
            gate.closePrompt = { [unowned self] in closed.append($0.id) }
            gate.persistAlwaysAllow = { [unowned self] in persisted.append($0) }
        }

        func request(_ session: String = "s1", connection: String = "lab") -> AccessRequest {
            AccessRequest(connectionID: connection, connectionName: "Lab PC", host: "10.211.55.9",
                          client: "claude-code 2.1.306", sessionID: session)
        }

        func check(_ session: String = "s1", connection: String = "lab", policy: AgentAccessPolicy = .ask) async -> AccessDecision {
            await gate.check(request(session, connection: connection), policy: policy)
        }
    }

    @Test func neverRefusesWithoutAsking() async {
        let harness = Harness()
        let decision = await harness.check(policy: .never)
        #expect(decision == .refused(AccessGate.neverMessage("Lab PC")))
        #expect(harness.shown.isEmpty)
    }

    @Test func alwaysAllowsWithoutAsking() async {
        let harness = Harness()
        #expect(await harness.check(policy: .always) == .allowed)
        #expect(harness.shown.isEmpty)
    }

    @Test func askThenDenyRefusesAndRemembersNothing() async {
        let harness = Harness()
        harness.answers = [.deny, .deny]
        #expect(await harness.check() == .refused("The user denied access to \"Lab PC\"."))
        #expect(await harness.check() != .allowed)
        #expect(harness.shown.count == 2)
        #expect(harness.closed == harness.shown.map(\.id))
        #expect(harness.gate.activeGrants.isEmpty)
    }

    @Test func promptNamesClientAndConnection() async {
        let harness = Harness()
        harness.answers = [.deny]
        _ = await harness.check()
        let request = harness.shown.first?.request
        #expect(request?.client == "claude-code 2.1.306")
        #expect(request?.connectionName == "Lab PC")
        #expect(request?.host == "10.211.55.9")
    }

    @Test func allowOnceCoversThatSessionUntilItEnds() async {
        let harness = Harness()
        harness.answers = [.allowOnce]
        #expect(await harness.check("s1") == .allowed)
        #expect(await harness.check("s1") == .allowed)
        #expect(harness.shown.count == 1)
        // Another session on the same connection asks for itself.
        harness.answers = [.deny]
        #expect(await harness.check("s2") != .allowed)
        #expect(harness.shown.count == 2)
        // Disconnecting ends the grant.
        harness.gate.sessionEnded("s1")
        harness.answers = [.deny]
        #expect(await harness.check("s1") != .allowed)
        #expect(harness.shown.count == 3)
    }

    @Test func tenMinutesCoverTheConnectionThenAskAgain() async throws {
        let harness = Harness()
        harness.answers = [.allowTenMinutes]
        #expect(await harness.check("s1") == .allowed)
        harness.date += 9 * 60
        #expect(await harness.check("s2") == .allowed)
        let grant = try #require(harness.gate.activeGrants.first { $0.connectionID == "lab" })
        #expect(grant.scope == .until(harness.date.addingTimeInterval(60)))
        harness.date += 60
        harness.answers = [.deny]
        let promptsBefore = harness.shown.count
        #expect(await harness.check("s1") != .allowed)
        #expect(harness.shown.count == promptsBefore + 1)
        #expect(harness.gate.activeGrants.isEmpty)
    }

    @Test func injectedGrantDurationIsUsed() async {
        let harness = Harness()
        harness.gate.timedGrantDuration = 3
        harness.answers = [.allowTenMinutes]
        #expect(await harness.check() == .allowed)
        harness.date += 2.9
        #expect(harness.gate.isGranted(harness.request()))
        harness.date += 0.1
        #expect(!harness.gate.isGranted(harness.request()))
    }

    @Test func todayLastsUntilLocalMidnight() async throws {
        let harness = Harness()
        harness.answers = [.allowToday]
        #expect(await harness.check() == .allowed)
        let grant = try #require(harness.gate.activeGrants.first)
        // 2026-10-15 00:00 in Berlin (CEST, +02:00) = 2026-10-14 22:00 UTC.
        #expect(grant.scope == .until(Date(timeIntervalSince1970: 1_792_015_200)))
        harness.date = Date(timeIntervalSince1970: 1_792_015_199)
        #expect(harness.gate.isGranted(harness.request()))
        harness.date = Date(timeIntervalSince1970: 1_792_015_200)
        #expect(!harness.gate.isGranted(harness.request()))
    }

    @Test func midnightAcrossTheDaylightSavingSwitch() {
        let harness = Harness()
        // 2026-10-25 01:00 Berlin (CEST); clocks go back at 03:00. Next midnight = 2026-10-26 00:00 CET = 23:00 UTC.
        let beforeSwitch = Date(timeIntervalSince1970: 1_792_882_800)
        #expect(harness.gate.nextMidnight(after: beforeSwitch) == Date(timeIntervalSince1970: 1_792_969_200))
    }

    @Test func alwaysAllowPersistsThePolicy() async {
        let harness = Harness()
        harness.answers = [.alwaysAllow]
        #expect(await harness.check() == .allowed)
        #expect(harness.persisted == ["lab"])
        #expect(harness.gate.activeGrants.isEmpty)
    }

    @Test func revokeAsksAgain() async throws {
        let harness = Harness()
        harness.answers = [.allowToday]
        #expect(await harness.check() == .allowed)
        let grant = try #require(harness.gate.activeGrants.first)
        harness.gate.revoke(grant.id)
        harness.answers = [.deny]
        #expect(await harness.check() != .allowed)
        #expect(harness.shown.count == 2)
    }

    @Test func parallelRequestsShareOnePrompt() async throws {
        let harness = Harness()
        async let first = harness.check("s1")
        async let second = harness.check("s2")
        async let third = harness.check("s3", connection: "other")
        // Let all three reach the gate.
        while harness.shown.count < 2 { await Task.yield() }
        try await Task.sleep(for: .milliseconds(20))
        #expect(harness.shown.count == 2)
        #expect(Set(harness.gate.openPrompts.map(\.request.connectionID)) == ["lab", "other"])
        let labPrompt = try #require(harness.shown.first { $0.request.connectionID == "lab" })
        let otherPrompt = try #require(harness.shown.first { $0.request.connectionID == "other" })
        harness.gate.answer(labPrompt.id, with: .allowTenMinutes)
        harness.gate.answer(otherPrompt.id, with: .deny)
        let results = await [first, second, third]
        #expect(results == [.allowed, .allowed, .refused("The user denied access to \"Lab PC\".")])
    }

    @Test func allowOnceForOneSessionMakesAWaitingSessionAskItself() async throws {
        let harness = Harness()
        async let first = harness.check("s1")
        async let second = harness.check("s2")
        while harness.shown.isEmpty { await Task.yield() }
        try await Task.sleep(for: .milliseconds(20))
        let prompt = try #require(harness.shown.first)
        harness.answers = [.deny]
        harness.gate.answer(prompt.id, with: .allowOnce)
        let results = await [first, second]
        #expect(results[0] == .allowed || results[1] == .allowed)
        #expect(results.contains(.refused("The user denied access to \"Lab PC\".")))
        #expect(harness.shown.count == 2)
    }

    @Test func noAnswerTimesOut() async {
        let harness = Harness(timeout: .milliseconds(50))
        let decision = await harness.check()
        guard case .refused(let message) = decision else {
            Issue.record("expected a refusal")
            return
        }
        #expect(message.hasPrefix("No answer from the user"))
        #expect(harness.gate.openPrompts.isEmpty)
        #expect(harness.closed.count == 1)
    }

    @Test func cancellingRefusesOpenPrompts() async throws {
        let harness = Harness()
        async let decision = harness.check()
        while harness.shown.isEmpty { await Task.yield() }
        harness.gate.cancelAllPrompts("AI agents were turned off.")
        #expect(await decision == .refused("AI agents were turned off."))
    }

    @Test func timeoutDescription() {
        #expect(AccessGate.describe(.seconds(120)) == "2 minutes")
        #expect(AccessGate.describe(.seconds(60)) == "1 minute")
        #expect(AccessGate.describe(.milliseconds(500)) == "0.5 seconds")
    }
}
