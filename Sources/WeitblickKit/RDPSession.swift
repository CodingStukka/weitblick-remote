import Foundation
import os
import WeitblickBridge

@MainActor
public protocol RDPSessionDelegate: AnyObject {
    func session(_ session: RDPSession, didReceive event: RDPSessionEvent)
    /// The server shows a certificate that is neither valid nor trusted for this connection. The
    /// connection waits for the answer (at most `RDPSession.certificateDecisionTimeout`, then it is
    /// rejected). Default: accept once and log.
    func session(_ session: RDPSession, decideAbout certificate: ServerCertificate) async -> CertificateDecision
}

extension RDPSessionDelegate {
    public func session(_ session: RDPSession, decideAbout certificate: ServerCertificate) async -> CertificateDecision {
        RDPSession.logAccepting(certificate)
        return .acceptOnce
    }
}

/// One RDP connection. Main-actor API over WeitblickBridge; events arrive via the delegate.
@MainActor
public final class RDPSession {
    public enum State: Equatable, Sendable {
        case idle, connecting, connected, reconnecting, disconnected
    }

    /// How long a connection waits for the delegate's certificate decision.
    public nonisolated static let certificateDecisionTimeout: TimeInterval = 120

    public weak var delegate: RDPSessionDelegate?
    public private(set) var state: State = .idle
    /// Why the session ended; set when `.disconnected` is delivered.
    public private(set) var disconnectReason: DisconnectReason?
    /// Current remote desktop size in pixels.
    public private(set) var desktopSize: PixelSize
    public let configuration: SessionConfiguration
    /// The clipboard channel (inactive unless `configuration.clipboard`).
    public let clipboard: RemoteClipboard

    private let native: SessionHandle
    private let certificates: CertificateGate
    private var isClosed = false

    /// The bridge session, or nil once closed (the bridge treats NULL as a no-op).
    var rawSession: OpaquePointer? { isClosed ? nil : native.raw }

    public init?(configuration: SessionConfiguration) {
        let clipboard = RemoteClipboard()
        let certificates = CertificateGate(trustedFingerprints: configuration.trustedCertificateFingerprints,
                                           timeout: Self.certificateDecisionTimeout)
        let relay = EventRelay(clipboard: clipboard, certificates: certificates)
        var callbacks = relay.makeCallbacks()
        try? FileManager.default.createDirectory(at: configuration.stateDirectory, withIntermediateDirectories: true)

        let strings = BridgeStrings()
        defer { strings.free() }
        let drives = configuration.drives.map {
            WBDrive(name: strings.copy($0.name), path: strings.copy($0.localPath.path))
        }
        var config = WBSessionConfig()
        config.host = strings.copy(configuration.host)
        config.port = configuration.port
        config.username = strings.copy(configuration.username)
        config.domain = strings.copy(configuration.domain)
        config.password = strings.copy(configuration.password)
        config.width = UInt32(configuration.desktopSize.width)
        config.height = UInt32(configuration.desktopSize.height)
        config.desktopScaleFactor = configuration.scale.desktop
        config.deviceScaleFactor = configuration.scale.device
        config.keyboardLayout = configuration.keyboardLayout
        config.ignoreCertificate = configuration.ignoreCertificate
        config.disableNLA = !configuration.nla
        config.consoleSession = configuration.consoleSession
        config.alternateShell = strings.copy(configuration.alternateShell)
        config.workingDirectory = strings.copy(configuration.workingDirectory)
        config.loadBalanceInfo = strings.copy(configuration.loadBalanceInfo)
        config.audio = configuration.audio.bridgeValue
        config.microphone = configuration.microphone
        config.printers = configuration.printers
        config.clipboard = configuration.clipboard
        config.autoReconnect = configuration.autoReconnect
        config.stateDirectory = strings.copy(configuration.stateDirectory.path)

        let created = drives.withUnsafeBufferPointer { list in
            config.drives = list.baseAddress
            config.driveCount = list.count
            return wb_session_create(&config, &callbacks)
        }
        guard let raw = created else { return nil }
        self.native = SessionHandle(raw: raw, relay: relay)
        self.configuration = configuration
        self.clipboard = clipboard
        self.certificates = certificates
        clipboard.attach(raw)
        self.desktopSize = configuration.desktopSize
        relay.session = self
    }

    deinit {
        certificates.close()
        clipboard.detach()
        native.destroyInBackground()
    }

    public func connect() {
        guard state == .idle else { return }
        state = wb_session_connect(rawSession) ? .connecting : .disconnected
    }

    /// Ends the session; `.disconnected` follows.
    public func disconnect() {
        certificates.close()
        wb_session_disconnect(rawSession)
    }

    /// Starts disconnecting and freeing the session in the background. Idempotent; every
    /// other call is a no-op afterwards.
    public func close() {
        isClosed = true
        certificates.close()
        clipboard.detach()
        native.destroyInBackground()
    }
    /// Closes the session and waits until its bridge thread is gone.
    public func closeAndWait() async {
        close()
        await native.waitUntilDestroyed()
    }

    /// Blocks until every session created so far is torn down. Call before the process exits:
    /// FreeRDP's threads use global state (WLog) that `exit()` destroys under them.
    @discardableResult
    public nonisolated static func waitForAllSessionsToClose(timeout: TimeInterval) -> Bool {
        SessionHandle.alive.wait(timeout: .now() + timeout) == .success
    }

    public func setResolution(_ size: PixelSize, scale: RemoteScale) {
        wb_session_set_resolution(rawSession, UInt32(size.width), UInt32(size.height), scale.desktop, scale.device)
    }

    /// Runs `body` with the framebuffer locked; nil if there is no framebuffer yet.
    /// Decoding waits while `body` runs, so keep it short.
    public func withFramebuffer<T>(_ body: (Framebuffer) throws -> T) rethrows -> T? {
        var raw = WBFramebuffer()
        guard wb_session_framebuffer_acquire(rawSession, &raw), let pixels = raw.pixels else { return nil }
        defer { wb_session_framebuffer_release(rawSession) }
        return try body(Framebuffer(
            pixels: pixels, width: Int(raw.width), height: Int(raw.height), bytesPerRow: Int(raw.stride),
            dirtyRects: UnsafeBufferPointer(start: raw.dirtyRects, count: raw.dirtyRectCount)))
    }

    /// Runs `body` with the framebuffer locked without taking the dirty rects (`dirtyRects` is
    /// empty) or re-arming `frameReady`: for readers besides the presenter, e.g. agent screenshots.
    public func peekFramebuffer<T>(_ body: (Framebuffer) throws -> T) rethrows -> T? {
        var raw = WBFramebuffer()
        guard wb_session_framebuffer_peek(rawSession, &raw), let pixels = raw.pixels else { return nil }
        defer { wb_session_framebuffer_release(rawSession) }
        return try body(Framebuffer(
            pixels: pixels, width: Int(raw.width), height: Int(raw.height), bytesPerRow: Int(raw.stride),
            dirtyRects: UnsafeBufferPointer(start: nil, count: 0)))
    }

    func handle(_ event: RDPSessionEvent) {
        switch event {
        case .connected, .reconnected: state = .connected
        case .reconnecting: state = .reconnecting
        case .disconnected: state = .disconnected
        case .desktopResized(let size): desktopSize = size
        case .frameReady, .pointer: break
        }
        delegate?.session(self, didReceive: event)
    }

    func ended(_ reason: DisconnectReason, code: UInt32, detail: String) {
        disconnectReason = reason
        if reason != .requested {
            Logger.session.notice("Session ended: \(String(describing: reason), privacy: .public) – \(detail, privacy: .public)")
        }
        handle(.disconnected(code: code, message: reason.message))
    }

    /// Asks the delegate about a certificate; `reply` runs exactly once.
    func ask(about certificate: ServerCertificate, reply: @escaping @Sendable (CertificateDecision) -> Void) {
        guard let delegate else {
            Self.logAccepting(certificate)
            return reply(.acceptOnce)
        }
        Task { reply(await delegate.session(self, decideAbout: certificate)) }
    }

    nonisolated static func logAccepting(_ certificate: ServerCertificate) {
        Logger.session.notice(
            "Accepting certificate of \(certificate.host, privacy: .public):\(certificate.port) (\(certificate.subject, privacy: .public)) sha256 \(certificate.fingerprint, privacy: .public)\(certificate.changed ? " [changed]" : "")")
    }
}

extension AudioPlayback {
    var bridgeValue: WBAudioMode {
        switch self {
        case .local: WBAudioLocal
        case .remote: WBAudioRemote
        case .off: WBAudioOff
        }
    }
}

/// C strings handed to the bridge for the duration of one call.
private final class BridgeStrings {
    private var pointers: [UnsafeMutablePointer<CChar>] = []

    func copy(_ string: String) -> UnsafePointer<CChar> {
        let pointer = strdup(string)!
        pointers.append(pointer)
        return UnsafePointer(pointer)
    }

    func free() {
        pointers.forEach { Darwin.free($0) }
        pointers = []
    }
}

/// Locked view of the remote framebuffer (BGRA32, top-down). Valid only inside `withFramebuffer`.
public struct Framebuffer {
    public let pixels: UnsafePointer<UInt8>
    public let width: Int
    public let height: Int
    public let bytesPerRow: Int
    /// Regions changed since the previous `withFramebuffer` call.
    public let dirtyRects: UnsafeBufferPointer<WBRect>
}

extension Logger {
    static let session = Logger(subsystem: "nrw.neuhaus.weitblick-remote", category: "session")
}

/// Owns the C session and the relay its callbacks point to; destroys both exactly once.
private final class SessionHandle: Sendable {
    /// Entered by every session until its teardown has finished.
    static let alive = DispatchGroup()

    nonisolated(unsafe) let raw: OpaquePointer
    private let relay: EventRelay
    private let teardownStarted = OSAllocatedUnfairLock(initialState: false)
    private let teardown = DispatchGroup()

    init(raw: OpaquePointer, relay: EventRelay) {
        self.raw = raw
        self.relay = relay
        Self.alive.enter()
        teardown.enter()
    }

    /// Joining the bridge thread can take a moment (TLS shutdown), so never on the main thread.
    func destroyInBackground() {
        let alreadyStarted = teardownStarted.withLock { started in
            defer { started = true }
            return started
        }
        guard !alreadyStarted else { return }
        DispatchQueue.global(qos: .userInitiated).async {
            wb_session_destroy(self.raw)
            withExtendedLifetime(self.relay) {}
            self.teardown.leave()
            Self.alive.leave()
        }
    }

    func waitUntilDestroyed() async {
        await withCheckedContinuation { continuation in
            teardown.notify(queue: .global()) { continuation.resume() }
        }
    }
}
