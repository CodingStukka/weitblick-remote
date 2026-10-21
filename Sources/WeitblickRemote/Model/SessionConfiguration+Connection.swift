import ConnectionStore
import CoreGraphics
import Foundation
import KeyboardEngine
import WeitblickKit

/// User name, domain and password for one connection attempt (from the Keychain or the sign-in sheet).
struct Credentials: Equatable, Sendable {
    var username: String
    var domain: String
    var password: String
}

extension SessionConfiguration {
    /// The one place where a saved connection becomes the settings of a session.
    /// The desktop size and scale come from the window (`DisplaySettings.desktop(…)`), the keyboard
    /// layout from the Mac unless the connection overrides it. FreeRDP keeps its state next to the
    /// connections file (`supportDirectory`), so test stores keep it out of the real folder.
    init(connection: Connection, credentials: Credentials, desktop: RemoteDesktop,
         macKeyboardLayout: WindowsKeyboardLayoutID,
         supportDirectory: URL = ConnectionStore.applicationSupportDirectory) {
        self.init(host: connection.host, username: credentials.username, password: credentials.password,
                  desktopSize: desktop.size)
        port = UInt16(clamping: connection.port)
        domain = credentials.domain
        scale = desktop.scale
        keyboardLayout = (connection.keyboard.layoutOverride ?? macKeyboardLayout).rawValue
        ignoreCertificate = connection.security.ignoreCertificateErrors
        trustedCertificateFingerprints = connection.security.trustedCertificateFingerprints
        nla = !connection.security.disableNLA
        consoleSession = connection.security.consoleSession
        alternateShell = connection.advanced.alternateShell ?? ""
        workingDirectory = connection.advanced.workingDir ?? ""
        loadBalanceInfo = connection.advanced.loadBalanceInfo ?? ""
        let redirection = connection.redirection
        audio = AudioPlayback(redirection.audioPlayback)
        microphone = redirection.microphone
        printers = redirection.printers
        clipboard = redirection.clipboard
        drives = redirection.driveRedirection ? redirection.drives.compactMap(SharedDrive.init) : []
        stateDirectory = supportDirectory.appending(path: "FreeRDP", directoryHint: .isDirectory)
    }
}

extension AudioPlayback {
    init(_ mode: AudioPlaybackMode) {
        switch mode {
        case .local: self = .local
        case .remote: self = .remote
        case .off: self = .off
        }
    }
}

extension SharedDrive {
    /// Enabled, writable mappings only: the session cannot share a folder read-only, so a mapping
    /// marked read-only is not shared at all rather than silently made writable.
    init?(_ mapping: DriveMapping) {
        guard mapping.enabled, !mapping.readOnly else { return nil }
        self.init(name: mapping.name, localPath: URL(fileURLWithPath: (mapping.localPath as NSString).expandingTildeInPath))
    }
}

/// Remote desktop size and Windows scaling for a session window.
struct RemoteDesktop: Equatable {
    var size: PixelSize
    var scale: RemoteScale
}

extension DisplaySettings {
    /// The remote desktop follows the window size (initially and on every resize).
    var followsWindow: Bool { matchScreenResolution }

    /// Resizes the remote desktop while the session runs.
    var resizesWithWindow: Bool { matchScreenResolution && dynamicResolution }

    /// Remote pixels per view point: Retina asks for one remote pixel per screen pixel.
    func pixelsPerPoint(backingScale: CGFloat) -> CGFloat {
        retina ? backingScale : 1
    }

    /// The desktop for a session view of `viewSize` points on a screen with `backingScale`.
    func desktop(forViewSize viewSize: CGSize, backingScale: CGFloat) -> RemoteDesktop {
        let density = pixelsPerPoint(backingScale: backingScale)
        let size = followsWindow
            ? PixelSize(width: Int((viewSize.width * density).rounded()), height: Int((viewSize.height * density).rounded()))
            : PixelSize(width: fixedWidth, height: fixedHeight)
        // RDP wants an even width; the bridge clamps to 200…8192 as well.
        let clamped = PixelSize(width: min(max(size.width, 200), 8192) & ~1, height: min(max(size.height, 200), 8192))
        let percent = desktopScaleFactor.map(UInt32.init) ?? UInt32((density * 100).rounded())
        return RemoteDesktop(size: clamped, scale: RemoteScale(desktop: min(max(percent, 100), 500), device: 100))
    }

    /// Window content size (points) that shows a fixed-size desktop 1:1.
    func contentSize(backingScale: CGFloat) -> CGSize? {
        guard !followsWindow else { return nil }
        let density = pixelsPerPoint(backingScale: backingScale)
        return CGSize(width: CGFloat(fixedWidth) / density, height: CGFloat(fixedHeight) / density)
    }
}
