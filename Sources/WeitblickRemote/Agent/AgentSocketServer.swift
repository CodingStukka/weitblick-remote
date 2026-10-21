import AgentControl
import Foundation
import os

/// The Unix domain socket `weitblick-mcp` connects to. Only this user may connect: the socket file
/// is mode 0600 and every peer's uid is checked. Lines go to the main actor; replies are written
/// from a queue per connection.
final class AgentSocketServer: @unchecked Sendable {
    enum Failure: Error, CustomStringConvertible {
        case pathTooLong(String)
        case inUse(String)
        case system(String, Int32)

        var description: String {
            switch self {
            case .pathTooLong(let path): "socket path too long: \(path)"
            case .inUse(let path): "another instance serves \(path)"
            case .system(let call, let code): "\(call) failed: \(String(cString: strerror(code)))"
            }
        }
    }

    let path: String
    private let queue = DispatchQueue(label: "nrw.neuhaus.weitblick-remote.agent-socket")
    private var listener: Int32 = -1
    private var acceptSource: DispatchSourceRead?
    private var nextID = 1
    /// A new peer of this user (called on the main actor).
    private let onConnect: @MainActor (AgentSocketConnection) -> Void

    private static let logger = Logger(subsystem: "nrw.neuhaus.weitblick-remote", category: "agent")

    init(path: String, onConnect: @escaping @MainActor (AgentSocketConnection) -> Void) {
        self.path = path
        self.onConnect = onConnect
    }

    func start() throws(Failure) {
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let capacity = MemoryLayout.size(ofValue: address.sun_path)
        guard path.utf8.count < capacity else { throw .pathTooLong(path) }
        withUnsafeMutableBytes(of: &address.sun_path) { buffer in
            buffer.copyBytes(from: path.utf8)
            buffer[path.utf8.count] = 0
        }
        try? FileManager.default.createDirectory(
            at: URL(fileURLWithPath: path).deletingLastPathComponent(), withIntermediateDirectories: true)
        if FileManager.default.fileExists(atPath: path) {
            // A live socket belongs to another instance (keep it); a dead one is left over.
            if Self.isServed(address) { throw .inUse(path) }
            unlink(path)
        }
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw .system("socket", errno) }
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        guard bound == 0 else {
            let code = errno
            close(fd)
            throw .system("bind", code)
        }
        guard chmod(path, 0o600) == 0, listen(fd, 8) == 0 else {
            let code = errno
            close(fd)
            unlink(path)
            throw .system("listen", code)
        }
        _ = fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK)
        listener = fd
        let source = DispatchSource.makeReadSource(fileDescriptor: fd, queue: queue)
        source.setEventHandler { [weak self] in self?.acceptPending() }
        source.setCancelHandler { close(fd) }
        source.resume()
        acceptSource = source
        Self.logger.notice("MCP socket listening at \(self.path, privacy: .public)")
    }

    /// Closes the listener and removes the socket file (open connections are closed by their owner).
    func stop() {
        guard let acceptSource else { return }
        self.acceptSource = nil
        acceptSource.cancel()
        listener = -1
        unlink(path)
        Self.logger.notice("MCP socket closed")
    }

    private func acceptPending() {
        while true {
            let fd = accept(listener, nil, nil)
            guard fd >= 0 else { return }
            var uid: uid_t = 0
            var gid: gid_t = 0
            guard getpeereid(fd, &uid, &gid) == 0, uid == getuid() else {
                Self.logger.error("refused an MCP peer of uid \(uid)")
                close(fd)
                continue
            }
            // Accepted sockets inherit O_NONBLOCK from the listener; replies are written blocking
            // on their own queue (a screenshot is larger than the socket buffer).
            _ = fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) & ~O_NONBLOCK)
            let connection = AgentSocketConnection(id: nextID, fd: fd)
            nextID += 1
            let onConnect = onConnect
            DispatchQueue.main.async {
                MainActor.assumeIsolated { onConnect(connection) }
            }
        }
    }

    private static func isServed(_ address: sockaddr_un) -> Bool {
        var address = address
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        return withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        } == 0
    }
}

/// One MCP client on the socket: newline-delimited JSON in both directions.
final class AgentSocketConnection: @unchecked Sendable {
    let id: Int
    private let fd: Int32
    private let readQueue: DispatchQueue
    private let writeQueue: DispatchQueue
    private var readSource: DispatchSourceRead?
    private var framer = LineFramer()
    private let lock = NSLock()
    private var closed = false

    init(id: Int, fd: Int32) {
        self.id = id
        self.fd = fd
        readQueue = DispatchQueue(label: "nrw.neuhaus.weitblick-remote.agent-client-\(id).read")
        writeQueue = DispatchQueue(label: "nrw.neuhaus.weitblick-remote.agent-client-\(id).write")
        var on: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size))
    }

    /// Starts reading; `onLine` gets every line, `onClose` once when the peer is gone (main actor).
    func start(onLine: @escaping @MainActor (String) -> Void, onClose: @escaping @MainActor () -> Void) {
        let fd = fd
        let writeQueue = writeQueue
        let source = DispatchSource.makeReadSource(fileDescriptor: fd, queue: readQueue)
        source.setEventHandler { [weak self] in
            guard let self else { return }
            var buffer = [UInt8](repeating: 0, count: 65536)
            let count = read(fd, &buffer, buffer.count)
            if count < 0, errno == EINTR || errno == EAGAIN { return }
            guard count > 0 else {
                self.close()
                DispatchQueue.main.async { MainActor.assumeIsolated(onClose) }
                return
            }
            do {
                let lines = try framer.append(buffer[0..<count])
                guard !lines.isEmpty else { return }
                DispatchQueue.main.async {
                    MainActor.assumeIsolated { lines.forEach(onLine) }
                }
            } catch {
                self.close()
                DispatchQueue.main.async { MainActor.assumeIsolated(onClose) }
            }
        }
        // The descriptor stays open until the source is gone and queued replies went out.
        source.setCancelHandler { writeQueue.async { Darwin.close(fd) } }
        readSource = source
        source.resume()
    }

    /// Sends one line (a newline is added). Thread safe; dropped once closed.
    func send(_ line: String) {
        var data = Data(line.utf8)
        data.append(0x0A)
        writeQueue.async { [self] in
            data.withUnsafeBytes { bytes in
                var offset = 0
                while offset < bytes.count {
                    if isClosed { return }
                    let written = write(fd, bytes.baseAddress! + offset, bytes.count - offset)
                    if written > 0 {
                        offset += written
                    } else if written < 0, errno == EINTR {
                        continue
                    } else {
                        return
                    }
                }
            }
        }
    }

    private var isClosed: Bool { lock.withLock { closed } }

    /// Closes the socket; the peer sees end of file.
    func close() {
        let wasOpen = lock.withLock {
            defer { closed = true }
            return !closed
        }
        guard wasOpen else { return }
        // Ends a write blocked on a peer that stopped reading; the descriptor closes after it.
        shutdown(fd, SHUT_RDWR)
        if let readSource {
            readSource.cancel()
        } else {
            let fd = fd
            writeQueue.async { Darwin.close(fd) }
        }
    }
}
