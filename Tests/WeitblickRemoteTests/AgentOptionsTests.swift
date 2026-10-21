import ConnectionStore
import XCTest

/// The launch options and overview state M7 (agent control) adds.
final class AgentOptionsTests: XCTestCase {
    func testAgentPathsDefaultToTheUserLibrary() {
        let options = LaunchOptions(arguments: ["app"], environment: [:])
        XCTAssertEqual(options.agentSocketURL, ConnectionStore.applicationSupportDirectory.appending(path: "mcp.sock"))
        XCTAssertTrue(options.agentLogURL.path.hasSuffix("/Library/Logs/Weitblick Remote/agent.log"))
        XCTAssertFalse(options.agentLaunch)
    }

    func testArgumentsWinOverTheEnvironment() {
        let options = LaunchOptions(
            arguments: ["app", "--agent-launch", "--mcp-socket", "/tmp/a.sock", "--agent-log", "/tmp/a.log"],
            environment: ["WEITBLICK_MCP_SOCKET": "/tmp/env.sock", "WEITBLICK_AGENT_LOG": "/tmp/env.log"])
        XCTAssertEqual(options.agentSocketURL.path, "/tmp/a.sock")
        XCTAssertEqual(options.agentLogURL.path, "/tmp/a.log")
        XCTAssertTrue(options.agentLaunch)
    }

    func testTheEnvironmentSetsThePathsForTheHelper() {
        let options = LaunchOptions(arguments: ["app"],
                                    environment: ["WEITBLICK_MCP_SOCKET": "/tmp/env.sock", "WEITBLICK_AGENT_LOG": "/tmp/env.log"])
        XCTAssertEqual(options.agentSocketURL.path, "/tmp/env.sock")
        XCTAssertEqual(options.agentLogURL.path, "/tmp/env.log")
    }

    @MainActor
    func testAnEndedSessionTakesItsAgentBadgeAlong() {
        let activity = SessionActivity()
        let id = UUID()
        activity.set(.connected, for: id)
        activity.setAgent(AgentActivity(client: "claude-code 2.1", paused: false, windowless: true), for: id)
        XCTAssertEqual(activity.agents[id]?.client, "claude-code 2.1")
        activity.set(nil, for: id)
        XCTAssertNil(activity.agents[id])
    }
}
