import Carbon
import Foundation
import KeyboardEngine

/// A Mac keyboard layout backed by the system's `uchr` data and `UCKeyTranslate`.
///
/// Text Input Sources calls (loading, `current()`, and `keyboardType` when no keyboard type code is
/// fixed) must happen on the main thread: TSM aborts when called from two threads at once.
/// With a fixed keyboard type code, translating touches no TSM state and is thread safe.
public struct UCKeyTranslateLayoutProvider: KeyboardLayoutProvider {
    /// e.g. `com.apple.keylayout.German`
    public let inputSourceID: String
    /// Languages of the input source (BCP 47), for the Windows layout fallback.
    public let languages: [String]
    private let layoutData: Data
    private let fixedKeyboardTypeCode: UInt32?
    private let fixedKeyboardType: PhysicalKeyboardType?

    /// Loads an installed layout by input source ID (enabled or not).
    /// - Parameter keyboardTypeCode: Carbon keyboard type (`LMGetKbdType()` values, e.g. 40 ANSI,
    ///   41 ISO, 42 JIS). nil = the type of the last used keyboard, read on every translation.
    public init?(inputSourceID: String, keyboardTypeCode: UInt32? = nil) {
        let filter = [kTISPropertyInputSourceID as String: inputSourceID] as CFDictionary
        guard let sources = TISCreateInputSourceList(filter, true)?.takeRetainedValue() as? [TISInputSource],
              let source = sources.first
        else { return nil }
        self.init(source: source, keyboardTypeCode: keyboardTypeCode)
    }

    /// The Mac layout that types like a Windows layout, on the keyboard type that layout is usually
    /// typed on (US: ANSI, Japanese: JIS, others: ISO). Falls back to US for layouts without a Mac
    /// counterpart. For resolving characters of remote shortcuts without a local key event.
    public init?(windowsLayout: WindowsKeyboardLayoutID) {
        let keyboardTypeCode: UInt32 = switch windowsLayout.typicalKeyboardType {
        case .ansi: 40
        case .iso: 41
        case .jis: 42
        }
        if let id = windowsLayout.macInputSourceID, let layout = Self(inputSourceID: id, keyboardTypeCode: keyboardTypeCode) {
            self = layout
        } else {
            self.init(inputSourceID: "com.apple.keylayout.US", keyboardTypeCode: keyboardTypeCode)
        }
    }

    /// The layout currently used for typing (for input methods: their underlying layout).
    public static func current(keyboardTypeCode: UInt32? = nil) -> UCKeyTranslateLayoutProvider? {
        let candidates = [
            TISCopyCurrentKeyboardLayoutInputSource()?.takeRetainedValue(),
            TISCopyCurrentASCIICapableKeyboardLayoutInputSource()?.takeRetainedValue(),
        ]
        return candidates.lazy.compactMap { $0 }.compactMap {
            UCKeyTranslateLayoutProvider(source: $0, keyboardTypeCode: keyboardTypeCode)
        }.first
    }

    private init?(source: TISInputSource, keyboardTypeCode: UInt32?) {
        guard let id: CFString = source.property(kTISPropertyInputSourceID),
              let data: CFData = source.property(kTISPropertyUnicodeKeyLayoutData)
        else { return nil }
        let languages: CFArray? = source.property(kTISPropertyInputSourceLanguages)
        inputSourceID = id as String
        self.languages = (languages as? [String]) ?? []
        layoutData = data as Data
        fixedKeyboardTypeCode = keyboardTypeCode
        fixedKeyboardType = keyboardTypeCode.map(PhysicalKeyboardType.init(macKeyboardType:))
    }

    public var keyboardType: PhysicalKeyboardType {
        fixedKeyboardType ?? PhysicalKeyboardType(macKeyboardType: keyboardTypeCode)
    }

    /// The Windows layout for a session typed with this Mac layout.
    public var windowsLayoutID: WindowsKeyboardLayoutID {
        WindowsKeyboardLayoutID(inputSourceID: inputSourceID, languages: languages)
    }

    public func translate(keyCode: UInt16, modifiers: LayoutModifiers, deadKeyState: inout UInt32) -> String {
        var carbonModifiers: UInt32 = 0
        if modifiers.contains(.command) { carbonModifiers |= UInt32(cmdKey) }
        if modifiers.contains(.shift) { carbonModifiers |= UInt32(shiftKey) }
        if modifiers.contains(.capsLock) { carbonModifiers |= UInt32(alphaLock) }
        if modifiers.contains(.option) { carbonModifiers |= UInt32(optionKey) }
        if modifiers.contains(.control) { carbonModifiers |= UInt32(controlKey) }

        var characters = [UniChar](repeating: 0, count: 16)
        var length = 0
        let status = layoutData.withUnsafeBytes { buffer -> OSStatus in
            guard let layout = buffer.baseAddress?.assumingMemoryBound(to: UCKeyboardLayout.self) else {
                return OSStatus(paramErr)
            }
            return UCKeyTranslate(
                layout, keyCode, UInt16(kUCKeyActionDown), (carbonModifiers >> 8) & 0xFF,
                keyboardTypeCode, 0, &deadKeyState, characters.count, &length, &characters
            )
        }
        guard status == noErr else {
            deadKeyState = 0
            return ""
        }
        // After composing, UCKeyTranslate keeps the finished dead key in the upper 16 bits (⌥N space
        // on German leaves 0x50000); only the lower bits mean a pending dead key. Report "none".
        if deadKeyState & 0xFFFF == 0 { deadKeyState = 0 }
        return String(utf16CodeUnits: characters, count: length)
    }

    private var keyboardTypeCode: UInt32 {
        fixedKeyboardTypeCode ?? UInt32(LMGetKbdType())
    }
}

extension PhysicalKeyboardType {
    /// From a Carbon keyboard type code (`LMGetKbdType()`, `CGEvent` field `keyboardEventKeyboardType`).
    public init(macKeyboardType code: UInt32) {
        switch KBGetLayoutType(Int16(truncatingIfNeeded: code)) {
        case OSType(kKeyboardISO): self = .iso
        case OSType(kKeyboardJIS): self = .jis
        default: self = .ansi
        }
    }
}

extension WindowsKeyboardLayoutID {
    /// The Windows layout matching the Mac input source currently used for typing.
    public static var current: WindowsKeyboardLayoutID {
        UCKeyTranslateLayoutProvider.current()?.windowsLayoutID ?? .usEnglish
    }
}

private extension TISInputSource {
    func property<T>(_ key: CFString) -> T? {
        guard let pointer = TISGetInputSourceProperty(self, key) else { return nil }
        return Unmanaged<AnyObject>.fromOpaque(pointer).takeUnretainedValue() as? T
    }
}
