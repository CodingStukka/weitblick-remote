/// A Windows keyboard layout identifier (KLID), e.g. `00000407` German. RDP sends it at connect.
public struct WindowsKeyboardLayoutID: RawRepresentable, Hashable, Sendable, CustomStringConvertible {
    public var rawValue: UInt32
    public init(rawValue: UInt32) { self.rawValue = rawValue }
    public init(_ rawValue: UInt32) { self.rawValue = rawValue }

    public static let usEnglish = WindowsKeyboardLayoutID(0x0000_0409)
    public static let german = WindowsKeyboardLayoutID(0x0000_0407)

    /// Windows notation: eight hex digits.
    public var description: String {
        (0..<4).reversed().map { hex(UInt8(truncatingIfNeeded: rawValue >> ($0 * 8))) }.joined()
    }

    /// Parses `00000407`, `0x407` or `407` (hex).
    public init?(notation: String) {
        let digits = notation.lowercased().hasPrefix("0x") ? notation.dropFirst(2) : Substring(notation)
        guard !digits.isEmpty, digits.count <= 8, let value = UInt32(digits, radix: 16) else { return nil }
        self.init(value)
    }

    /// The Windows layout matching a macOS input source ID (`com.apple.keylayout.German` -> 00000407).
    /// Unknown IDs fall back to the input source's languages, then to US English.
    public init(inputSourceID: String, languages: [String] = []) {
        if let known = layoutsByName[Self.layoutName(of: inputSourceID)] {
            self.init(known)
        } else if let known = languages.lazy.compactMap({ layoutsByLanguage[Self.primaryLanguage($0)] }).first {
            self.init(known)
        } else {
            self = .usEnglish
        }
    }

    /// The macOS keyboard layout that types like this Windows layout (`00000407` ->
    /// `com.apple.keylayout.German`), nil for layouts without a known Mac counterpart.
    public var macInputSourceID: String? {
        layoutNames.first { $0.value == rawValue }.map { "com.apple.keylayout." + $0.key }
    }

    /// The physical keyboard this layout is usually typed on: ANSI for US English layouts, JIS for
    /// Japanese, ISO for the rest. Decides where the two keys ANSI and ISO keyboards differ in go.
    public var typicalKeyboardType: PhysicalKeyboardType {
        switch rawValue & 0xFFFF {
        case 0x0409: .ansi
        case 0x0411: .jis
        default: .iso
        }
    }

    /// `com.apple.keylayout.German` -> `German`, `…keylayout.LogitechGerman` -> `German`.
    private static func layoutName(of inputSourceID: String) -> String {
        let name = inputSourceID.split(separator: ".").last.map(String.init) ?? inputSourceID
        return name.hasPrefix("Logitech") ? String(name.dropFirst("Logitech".count)) : name
    }

    private static func primaryLanguage(_ tag: String) -> String {
        String(tag.lowercased().split(whereSeparator: { $0 == "-" || $0 == "_" }).first ?? "")
    }
}

extension WindowsKeyboardLayoutID: Codable {
    public init(from decoder: any Decoder) throws {
        let container = try decoder.singleValueContainer()
        let notation = try container.decode(String.self)
        guard let id = WindowsKeyboardLayoutID(notation: notation) else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "invalid KLID '\(notation)'")
        }
        self = id
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(description)
    }
}

/// macOS layout name -> KLID. Names are the last component of the input source ID; the first name
/// listed for a KLID is the one `macInputSourceID` picks.
private let layoutNames: KeyValuePairs<String, UInt32> = [
    // German-speaking (Windows has no separate Austrian layout)
    "German": 0x0407, "Austrian": 0x0407, "German-DIN-2137": 0x0407, "ABC-QWERTZ": 0x0407,
    "SwissGerman": 0x0807, "SwissFrench": 0x100C,
    // English
    "US": 0x0409, "ABC": 0x0409, "USExtended": 0x0409, "Australian": 0x0409, "Canadian": 0x0409,
    "NewZealand": 0x0409, "ABC-India": 0x0409,
    "USInternational-PC": 0x0002_0409, "USIntl": 0x0002_0409, "Brazilian-Pro": 0x0002_0409,
    "Dvorak": 0x0001_0409, "DVORAK-QWERTYCMD": 0x0001_0409, "Dvorak-Left": 0x0003_0409,
    "Dvorak-Right": 0x0004_0409,
    "British": 0x0809, "British-PC": 0x0809, "UKIntl": 0x0452, "Welsh": 0x0452,
    "Irish": 0x1809, "IrishExtended": 0x1809,
    // Romance
    "French": 0x040C, "French-PC": 0x040C, "French-numerical": 0x040C, "ABC-AZERTY": 0x040C,
    "Belgian": 0x080C, "CanadianFrench-PC": 0x1009, "Canadian-CSA": 0x0001_1009,
    "Italian": 0x0410, "Italian-Pro": 0x0410,
    "Spanish": 0x040A, "Spanish-ISO": 0x040A, "LatinAmerican": 0x080A,
    "Portuguese": 0x0816, "Brazilian": 0x0001_0416, "Brazilian-ABNT2": 0x0001_0416,
    "Romanian": 0x0001_0418, "Romanian-Standard": 0x0001_0418,
    "Dutch": 0x0413,
    // Nordic
    "Danish": 0x0406, "Norwegian": 0x0414, "NorwegianExtended": 0x0414,
    "Swedish": 0x041D, "Swedish-Pro": 0x041D, "Finnish": 0x040B, "FinnishExtended": 0x040B,
    "Icelandic": 0x040F, "Faroese": 0x0438,
    // Central and Eastern Europe
    "Polish": 0x0001_0415, "PolishPro": 0x0415,
    "Czech": 0x0405, "Czech-QWERTY": 0x0001_0405, "Slovak": 0x041B, "Slovak-QWERTY": 0x0001_041B,
    "Hungarian": 0x040E, "Hungarian-QWERTY": 0x0001_040E,
    "Croatian": 0x041A, "Croatian-PC": 0x041A, "Slovenian": 0x0424,
    "Serbian": 0x0C1A, "Serbian-Latin": 0x081A, "Macedonian": 0x042F, "Albanian": 0x041C,
    "Bulgarian": 0x0003_0402, "Bulgarian-Phonetic": 0x0004_0402,
    "Estonian": 0x0425, "Latvian": 0x0002_0426, "Lithuanian": 0x0001_0427, "Lithuanian-LST1582": 0x0002_0427,
    "Maltese": 0x043A,
    // Cyrillic, Greek, Turkish
    "Russian": 0x0419, "RussianWin": 0x0419, "Russian-Phonetic": 0x0002_0419,
    "Ukrainian": 0x0422, "Ukrainian-QWERTY": 0x0422, "Ukrainian-PC": 0x0002_0422,
    "Byelorussian": 0x0423, "Kazakh": 0x043F,
    "Greek": 0x0408, "GreekPolytonic": 0x0006_0408,
    "Turkish": 0x0001_041F, "Turkish-Standard": 0x0001_041F, "Turkish-QWERTY": 0x041F,
    "Turkish-QWERTY-PC": 0x041F,
    // Others
    "Hebrew": 0x040D, "Hebrew-PC": 0x040D, "Hebrew-QWERTY": 0x040D,
    "Arabic": 0x0401, "ArabicPC": 0x0401, "Persian": 0x0429,
    "Vietnamese": 0x042A, "Thai": 0x041E, "2SetHangul": 0x0412, "KANA": 0x0411,
]

private let layoutsByName = Dictionary(layoutNames.map { ($0.key, $0.value) }, uniquingKeysWith: { first, _ in first })

/// Primary language subtag -> KLID of that language's standard Windows layout.
private let layoutsByLanguage: [String: UInt32] = [
    "de": 0x0407, "en": 0x0409, "fr": 0x040C, "it": 0x0410, "es": 0x040A, "pt": 0x0816,
    "nl": 0x0413, "da": 0x0406, "nb": 0x0414, "nn": 0x0414, "no": 0x0414, "sv": 0x041D,
    "fi": 0x040B, "is": 0x040F, "pl": 0x0415, "cs": 0x0405, "sk": 0x041B, "hu": 0x040E,
    "hr": 0x041A, "sl": 0x0424, "ro": 0x0001_0418, "bg": 0x0003_0402, "et": 0x0425,
    "lv": 0x0002_0426, "lt": 0x0001_0427, "tr": 0x041F, "el": 0x0408, "ru": 0x0419,
    "uk": 0x0422, "he": 0x040D, "ar": 0x0401, "ko": 0x0412, "ja": 0x0411,
]
