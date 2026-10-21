import Foundation
import KeyboardEngine

public enum ConnectionProtocol: String, Codable, Hashable, Sendable {
    case rdp, vnc
}

public struct Connection: Codable, Hashable, Sendable, Identifiable {
    public static let currentSchemaVersion = 1
    public var schemaVersion = currentSchemaVersion
    public var id: UUID
    public var name: String
    public var host: String
    public var port: Int
    public var username: String
    public var domain: String
    public var `protocol`: ConnectionProtocol
    public var display = DisplaySettings()
    public var redirection = RedirectionSettings()
    public var keyboard = KeyboardConfig()
    public var security = SecuritySettings()
    public var advanced = AdvancedSettings()
    public var tags: [String] = []
    public var lastConnected: Date?
    public var notes = ""
    public var importSource: ImportSource?

    public init(id: UUID = UUID(), name: String, host: String, port: Int = 3389,
                username: String = "", domain: String = "", protocol: ConnectionProtocol = .rdp) {
        self.id = id
        self.name = name
        self.host = host
        self.port = port
        self.username = username
        self.domain = domain
        self.protocol = `protocol`
    }

    /// Only VNC connections can be handed off to macOS Screen Sharing. No credentials in the URL.
    public var vncURL: URL? {
        guard `protocol` == .vnc else { return nil }
        var components = URLComponents()
        components.scheme = "vnc"
        components.host = host.contains(":") && !host.hasPrefix("[") ? "[\(host)]" : host
        components.port = port
        return components.url
    }

    public func validate() throws {
        guard schemaVersion == Self.currentSchemaVersion else { throw ConnectionStoreError.unsupportedSchema(schemaVersion) }
        guard !host.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              (1...65535).contains(port) else { throw ConnectionStoreError.invalidConnection(id) }
        guard display.fixedWidth > 0, display.fixedHeight > 0, display.monitorCount > 0,
              display.desktopScaleFactor.map({ (100...500).contains($0) }) ?? true else {
            throw ConnectionStoreError.invalidConnection(id)
        }
    }
}

public struct DisplaySettings: Codable, Hashable, Sendable {
    public var dynamicResolution = true
    public var matchScreenResolution = true
    public var retina = false
    public var fixedWidth = 1024
    public var fixedHeight = 768
    public var startFullscreen = false
    public var useAllMonitors = false
    public var monitorCount = 1
    /// nil means automatic; Jump stores this as zero.
    public var desktopScaleFactor: Int?
    public init() {}
}

public enum AudioPlaybackMode: String, Codable, Hashable, Sendable {
    case local, remote, off
}

public struct RedirectionSettings: Codable, Hashable, Sendable {
    public var clipboard = true
    public var audioPlayback: AudioPlaybackMode = .local
    public var microphone = false
    public var audioInputDevice: String?
    public var printers = true
    public var defaultPrinter: String?
    /// Master switch is independent of the individual drive's Enabled flag.
    public var driveRedirection = false
    public var drives: [DriveMapping] = []
    public init() {}
}

public struct DriveMapping: Codable, Hashable, Sendable {
    public var name: String
    public var localPath: String
    public var enabled: Bool
    public var readOnly: Bool
    public var sourceID: String?
    public init(name: String, localPath: String, enabled: Bool = true, readOnly: Bool = false,
                sourceID: String? = nil) {
        self.name = name
        self.localPath = localPath
        self.enabled = enabled
        self.readOnly = readOnly
        self.sourceID = sourceID
    }
}

public struct SecuritySettings: Codable, Hashable, Sendable {
    public var ignoreCertificateErrors = false
    public var trustedCertificateFingerprints: [String] = []
    public var disableNLA = false
    public var consoleSession = false
    /// RDP authentication level: 0 = allow, 1 = require, 2 = warn; retained for .rdp round trips.
    public var authenticationLevel = 2
    public var promptForCredentials = true
    /// Whether AI agents (MCP) may use this connection.
    public var agentAccess = AgentAccess.ask
    public init() {}

    private enum CodingKeys: String, CodingKey {
        case ignoreCertificateErrors, trustedCertificateFingerprints, disableNLA, consoleSession
        case authenticationLevel, promptForCredentials, agentAccess
    }

    /// Missing keys (stores written by older versions) keep their defaults; an unknown agent access
    /// value falls back to asking.
    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        ignoreCertificateErrors = try container.decodeIfPresent(Bool.self, forKey: .ignoreCertificateErrors) ?? false
        trustedCertificateFingerprints = try container.decodeIfPresent([String].self, forKey: .trustedCertificateFingerprints) ?? []
        disableNLA = try container.decodeIfPresent(Bool.self, forKey: .disableNLA) ?? false
        consoleSession = try container.decodeIfPresent(Bool.self, forKey: .consoleSession) ?? false
        authenticationLevel = try container.decodeIfPresent(Int.self, forKey: .authenticationLevel) ?? 2
        promptForCredentials = try container.decodeIfPresent(Bool.self, forKey: .promptForCredentials) ?? true
        agentAccess = (try? container.decodeIfPresent(AgentAccess.self, forKey: .agentAccess)) ?? .ask
    }

    /// Compares hex digits only, so `AA:BB…`, `aabb…` and `AA BB …` are the same fingerprint.
    public func trusts(fingerprint: String) -> Bool {
        let wanted = Self.normalizedFingerprint(fingerprint)
        return !wanted.isEmpty && trustedCertificateFingerprints.contains { Self.normalizedFingerprint($0) == wanted }
    }

    public static func normalizedFingerprint(_ fingerprint: String) -> String {
        String(fingerprint.lowercased().filter(\.isHexDigit))
    }
}

/// A connection's "AI agent access" (MCP): ask in a permission prompt, always allow, or never.
public enum AgentAccess: String, Codable, Hashable, Sendable, CaseIterable {
    case ask, always, never
}

public struct AdvancedSettings: Codable, Hashable, Sendable {
    public var alternateShell: String?
    public var workingDir: String?
    public var loadBalanceInfo: String?
    public var wakeOnLANMACAddresses: [String] = []
    public var gatewayRef: String?
    public var gatewayHostname: String?
    /// Windows drive selectors from .rdp, not local paths. Never turn '*' into local drive access.
    public var rdpDriveStoreDirect: String?
    public init() {}
}

public struct ImportSource: Codable, Hashable, Sendable {
    public var kind: String
    public var uniqueID: String
    public var fileName: String
    public var importedAt: Date
    public init(kind: String = "jump", uniqueID: String, fileName: String, importedAt: Date = Date()) {
        self.kind = kind
        self.uniqueID = uniqueID
        self.fileName = fileName
        self.importedAt = importedAt
    }
}
