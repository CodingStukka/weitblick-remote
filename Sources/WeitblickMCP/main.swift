import AppKit
import Darwin
import Foundation

// weitblick-mcp: the MCP server Claude Code and other clients start (stdio). It relays stdin and
// stdout to the Unix socket Weitblick Remote serves while Settings → "AI agents (MCP)" is on, and
// launches the app hidden when it isn't running. All MCP logic lives in the app.
//
//     claude mcp add weitblick -- "/Applications/Weitblick Remote.app/Contents/MacOS/weitblick-mcp"
//
//     weitblick-mcp [--socket <path>]
//
// Environment: WEITBLICK_MCP_SOCKET (socket path), WEITBLICK_MCP_APP (an executable started
// instead of the enclosing app, for tests; it gets --agent-launch and --mcp-socket).

let defaultsKey = "agentControlEnabled"
let launchTimeout: TimeInterval = 30
let turnedOffMessage = "AI agents are turned off in Weitblick Remote. Turn them on in Weitblick Remote → Settings → AI agents (MCP)."

let environment = ProcessInfo.processInfo.environment
let arguments = CommandLine.arguments

if arguments.contains("--help") || arguments.contains("-h") {
    print("""
        weitblick-mcp: MCP server (stdio) for Weitblick Remote's agent control.
        Usage: weitblick-mcp [--socket <path>]
        Set up Claude Code: claude mcp add weitblick -- "\(Bundle.main.executablePath ?? "weitblick-mcp")"
        """)
    exit(0)
}

signal(SIGPIPE, SIG_IGN)

let defaultSocketPath = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/Weitblick Remote/mcp.sock").path

let socketPath: String = {
    if let index = arguments.firstIndex(of: "--socket"), arguments.indices.contains(index + 1) {
        return arguments[index + 1]
    }
    if let path = environment["WEITBLICK_MCP_SOCKET"], !path.isEmpty { return path }
    return defaultSocketPath
}()

/// The app this helper sits in (`….app/Contents/MacOS/weitblick-mcp`).
let enclosingApp: URL? = {
    guard let executable = Bundle.main.executableURL?.resolvingSymlinksInPath() else { return nil }
    let app = executable.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    return app.pathExtension == "app" ? app : nil
}()

/// A connected socket, or nil if nobody listens.
func connectSocket(_ path: String) -> Int32? {
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    guard fd >= 0 else { return nil }
    var address = sockaddr_un()
    address.sun_family = sa_family_t(AF_UNIX)
    let bytes = Array(path.utf8)
    guard bytes.count < MemoryLayout.size(ofValue: address.sun_path) else {
        close(fd)
        return nil
    }
    withUnsafeMutableBytes(of: &address.sun_path) { target in
        target.copyBytes(from: bytes)
        target[bytes.count] = 0
    }
    address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
    let connected = withUnsafePointer(to: &address) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
        }
    }
    guard connected == 0 else {
        close(fd)
        return nil
    }
    var on: Int32 = 1
    setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size))
    return fd
}

func writeAll(_ fd: Int32, _ buffer: UnsafeRawPointer, _ count: Int) -> Bool {
    var offset = 0
    while offset < count {
        let written = write(fd, buffer + offset, count - offset)
        if written < 0, errno == EINTR { continue }
        guard written > 0 else { return false }
        offset += written
    }
    return true
}

func printError(_ message: String) {
    FileHandle.standardError.write(Data("weitblick-mcp: \(message)\n".utf8))
}

/// No app to talk to: answer every request with the reason until the client gives up.
func serveError(_ message: String) -> Never {
    printError(message)
    while let line = readLine(strippingNewline: true) {
        guard let data = line.data(using: .utf8),
              let request = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let id = request["id"], request["method"] != nil else { continue }
        let reply: [String: Any] = ["jsonrpc": "2.0", "id": id, "error": ["code": -32000, "message": message]]
        if let encoded = try? JSONSerialization.data(withJSONObject: reply) {
            FileHandle.standardOutput.write(encoded + Data("\n".utf8))
        }
    }
    exit(1)
}

/// The master switch as the app sees it (its defaults domain is its bundle identifier).
func agentsEnabled(bundleID: String) -> Bool {
    CFPreferencesAppSynchronize(bundleID as CFString)
    let value = CFPreferencesCopyAppValue(defaultsKey as CFString, bundleID as CFString)
    return (value as? NSNumber)?.boolValue ?? false
}

/// Starts the app for agents; returns a check whether it still runs.
func launchApp() -> (() -> Bool)? {
    if let executable = environment["WEITBLICK_MCP_APP"], !executable.isEmpty {
        return spawn(executable, ["--agent-launch", "--mcp-socket", socketPath])
    }
    guard let app = enclosingApp, let bundleID = Bundle(url: app)?.bundleIdentifier else { return nil }
    if !NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).isEmpty {
        // Running without the socket: never reopen it (that could show or hide the user's windows).
        return { !NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).isEmpty }
    }
    let configuration = NSWorkspace.OpenConfiguration()
    configuration.activates = false
    configuration.hides = true
    configuration.addsToRecentItems = false
    configuration.createsNewApplicationInstance = false
    configuration.arguments = ["--agent-launch"] + (socketPath == defaultSocketPath ? [] : ["--mcp-socket", socketPath])
    let opened = DispatchSemaphore(value: 0)
    nonisolated(unsafe) var failure: (any Error)?
    NSWorkspace.shared.openApplication(at: app, configuration: configuration) { _, error in
        failure = error
        opened.signal()
    }
    guard opened.wait(timeout: .now() + launchTimeout) == .success, failure == nil else {
        printError("Weitblick Remote couldn't be launched: \(failure.map { String(describing: $0) } ?? "timeout")")
        return nil
    }
    return { !NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).isEmpty }
}

/// Tests: the given executable in its own session (it outlives this helper), output discarded.
func spawn(_ executable: String, _ extra: [String]) -> (() -> Bool)? {
    var attributes: posix_spawnattr_t?
    posix_spawnattr_init(&attributes)
    defer { posix_spawnattr_destroy(&attributes) }
    posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_SETSID))
    var actions: posix_spawn_file_actions_t?
    posix_spawn_file_actions_init(&actions)
    defer { posix_spawn_file_actions_destroy(&actions) }
    posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0)
    posix_spawn_file_actions_addopen(&actions, 1, "/dev/null", O_WRONLY, 0)
    posix_spawn_file_actions_addopen(&actions, 2, "/dev/null", O_WRONLY, 0)
    let argv = ([executable] + extra).map { strdup($0) } + [nil]
    defer { argv.forEach { free($0) } }
    var pid: pid_t = 0
    let status = posix_spawn(&pid, executable, &actions, &attributes, argv, environ)
    guard status == 0 else {
        printError("couldn't start \(executable): \(String(cString: strerror(status)))")
        return nil
    }
    return {
        var exitStatus: Int32 = 0
        return waitpid(pid, &exitStatus, WNOHANG) == 0
    }
}

func waitForSocket(stillRunning: () -> Bool) -> Int32? {
    let deadline = Date().addingTimeInterval(launchTimeout)
    while Date() < deadline {
        if let fd = connectSocket(socketPath) { return fd }
        guard stillRunning() else { return connectSocket(socketPath) }
        usleep(200_000)
    }
    return nil
}

/// Copies stdin to the socket and the socket to stdout until either side ends.
func relay(_ fd: Int32) -> Never {
    let upstream = Thread {
        let buffer = UnsafeMutableRawPointer.allocate(byteCount: 65_536, alignment: 1)
        while true {
            let count = read(0, buffer, 65_536)
            if count < 0, errno == EINTR { continue }
            guard count > 0, writeAll(fd, buffer, count) else { break }
        }
        // The client is done: the app sees end of file, cleans up and closes; don't wait forever.
        shutdown(fd, SHUT_WR)
        sleep(3)
        exit(0)
    }
    upstream.start()
    let buffer = UnsafeMutableRawPointer.allocate(byteCount: 65_536, alignment: 1)
    while true {
        let count = read(fd, buffer, 65_536)
        if count < 0, errno == EINTR { continue }
        guard count > 0, writeAll(1, buffer, count) else { break }
    }
    exit(0)
}

if let fd = connectSocket(socketPath) { relay(fd) }

// Nobody listens. With agents turned off the app wouldn't serve MCP, so don't start it.
if environment["WEITBLICK_MCP_APP"] == nil {
    guard let app = enclosingApp, let bundleID = Bundle(url: app)?.bundleIdentifier else {
        serveError("Weitblick Remote isn't running and this helper isn't inside the app, so it can't start it (socket \(socketPath)).")
    }
    guard agentsEnabled(bundleID: bundleID) else { serveError(turnedOffMessage) }
}
guard let stillRunning = launchApp() else { serveError("Weitblick Remote couldn't be started.") }
guard let fd = waitForSocket(stillRunning: stillRunning) else {
    serveError(stillRunning()
        ? "Weitblick Remote runs but doesn't serve agents. Check Settings → AI agents (MCP) in Weitblick Remote."
        : turnedOffMessage)
}
relay(fd)
