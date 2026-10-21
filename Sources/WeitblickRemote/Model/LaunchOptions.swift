import ConnectionStore
import Foundation

/// Where the app reads and writes its data. Tests and dry runs point it elsewhere with
/// `--store <file>` / `--jump-dir <folder>` / `--jump-profile <file>` / `--mcp-socket <file>` /
/// `--agent-log <file>` or the environment variables `WEITBLICK_STORE`, `WEITBLICK_JUMP_DIR`,
/// `WEITBLICK_JUMP_PROFILE`, `WEITBLICK_MCP_SOCKET`, `WEITBLICK_AGENT_LOG`.
struct LaunchOptions {
    var storeURL = ConnectionStore.defaultFileURL
    var jumpDirectory = JumpImporter.defaultDirectory
    var jumpInputProfileURL = JumpInputProfileImporter.defaultFileURL
    /// The MCP socket (`weitblick-mcp` relays to it).
    var agentSocketURL = ConnectionStore.applicationSupportDirectory.appending(path: "mcp.sock")
    /// The audit log of agent tool calls.
    var agentLogURL = FileManager.default.homeDirectoryForCurrentUser
        .appending(path: "Library/Logs/Weitblick Remote/agent.log")
    /// Started by `weitblick-mcp` (`--agent-launch`): no windows, quits again when no agent needs it.
    var agentLaunch = false
    /// All command-line arguments, for the DEBUG hooks.
    var arguments: [String]

    init(arguments: [String] = CommandLine.arguments, environment: [String: String] = ProcessInfo.processInfo.environment) {
        self.arguments = arguments
        if let path = Self.value(of: "--store", in: arguments) ?? environment["WEITBLICK_STORE"] {
            storeURL = URL(fileURLWithPath: path)
        }
        if let path = Self.value(of: "--jump-dir", in: arguments) ?? environment["WEITBLICK_JUMP_DIR"] {
            jumpDirectory = URL(fileURLWithPath: path, isDirectory: true)
        }
        if let path = Self.value(of: "--jump-profile", in: arguments) ?? environment["WEITBLICK_JUMP_PROFILE"] {
            jumpInputProfileURL = URL(fileURLWithPath: path)
        }
        if let path = Self.value(of: "--mcp-socket", in: arguments) ?? environment["WEITBLICK_MCP_SOCKET"] {
            agentSocketURL = URL(fileURLWithPath: path)
        }
        if let path = Self.value(of: "--agent-log", in: arguments) ?? environment["WEITBLICK_AGENT_LOG"] {
            agentLogURL = URL(fileURLWithPath: path)
        }
        agentLaunch = arguments.contains("--agent-launch")
    }

    /// The argument following `flag`, if any.
    static func value(of flag: String, in arguments: [String]) -> String? {
        guard let index = arguments.firstIndex(of: flag), arguments.indices.contains(index + 1) else { return nil }
        return arguments[index + 1]
    }
}
