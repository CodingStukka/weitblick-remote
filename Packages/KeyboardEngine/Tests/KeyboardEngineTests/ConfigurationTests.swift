import Foundation
@testable import KeyboardEngine
import KeyboardEngineCarbon
import Testing

/// JSON configuration, shortcut notation, KLID mapping and event flag conversion.
struct ConfigurationTests {
    @Test func defaultConfigRoundTripsThroughJSON() throws {
        var config = KeyboardConfig()
        config.optionStrategy = .jumpStyle
        config.keyboardTypeOverride = .iso
        config.layoutOverride = WindowsKeyboardLayoutID(0x0807)
        let data = try JSONEncoder().encode(config)
        #expect(try JSONDecoder().decode(KeyboardConfig.self, from: data) == config)
    }

    @Test func missingKeysDecodeToDefaults() throws {
        let config = try JSONDecoder().decode(KeyboardConfig.self, from: Data(#"{"mode": "windowsDirect"}"#.utf8))
        var expected = KeyboardConfig()
        expected.mode = .windowsDirect
        #expect(config == expected)
    }

    @Test func rulesAreReadableJSON() throws {
        let json = #"""
        {
          "rules": [
            {"mac": "cmd+left", "windows": ["home"], "keepShift": true},
            {"mac": "cmd+e", "windows": ["win+e"]},
            {"mac": "cmd+tab", "windows": ["alt+tab"], "keepShift": true, "holdUntilRelease": true},
            {"mac": "cmd+[", "windows": ["alt+left"], "typedCharacter": true}
          ],
          "layoutOverride": "00000807",
          "reservedShortcuts": ["cmd+q", "ctrl+opt+cmd+*"]
        }
        """#
        let config = try JSONDecoder().decode(KeyboardConfig.self, from: Data(json.utf8))
        #expect(config.rules == [
            ShortcutRule("cmd+left", ["home"], keepShift: true),
            ShortcutRule("cmd+e", ["win+e"]),
            ShortcutRule("cmd+tab", ["alt+tab"], keepShift: true, holdUntilRelease: true),
            ShortcutRule("cmd+[", ["alt+left"], typedCharacter: true),
        ])
        #expect(config.layoutOverride == WindowsKeyboardLayoutID(0x0807))
        #expect(config.reservedShortcuts.map(\.description) == ["cmd+q", "ctrl+opt+cmd+*"])

        let encoded = String(decoding: try JSONEncoder().encode(config.rules[0]), as: UTF8.self)
        #expect(encoded.contains(#""mac":"cmd+left""#) && encoded.contains(#""windows":["home"]"#))
        let typed = String(decoding: try JSONEncoder().encode(config.rules[3]), as: UTF8.self)
        #expect(typed.contains(#""typedCharacter":true"#))
    }

    @Test(arguments: [
        #"{"rules": [{"mac": "cmd+nonsense", "windows": ["home"]}]}"#,
        #"{"rules": [{"mac": "win+x", "windows": ["home"]}]}"#,
        #"{"rules": [{"mac": "cmd+x", "windows": []}]}"#,
        #"{"rules": [{"mac": "cmd+x", "windows": ["cmd+x"]}]}"#,
        #"{"rules": [{"mac": "cmd+tab", "windows": ["alt+tab", "tab"], "holdUntilRelease": true}]}"#,
        #"{"rules": [{"mac": "shift+tab", "windows": ["alt+tab"], "holdUntilRelease": true}]}"#,
        #"{"rules": [{"mac": "cmd+left", "windows": ["home"], "typedCharacter": true}]}"#,
        #"{"rules": [{"mac": "cmd+[", "windows": ["home"], "typedCharacter": true, "keepShift": true}]}"#,
        #"{"reservedShortcuts": ["cmd+printscreen"]}"#,
        #"{"layoutOverride": "German"}"#,
    ])
    func invalidConfigurationIsRejected(json: String) {
        #expect(throws: DecodingError.self) {
            try JSONDecoder().decode(KeyboardConfig.self, from: Data(json.utf8))
        }
    }

    @Test func notationIsCanonical() throws {
        #expect(try MacChord("Command+Shift+Z").description == "shift+cmd+z")
        #expect(try MacChord("ctrl+opt+cmd+*").key == .any)
        #expect(try MacChord("⌘+q") == MacChord("cmd+Q"))
        #expect(try MacChord("cmd+plus").key == .character("+"))
        #expect(try MacChord("cmd+plus").description == "cmd+plus")
        #expect(try WindowsChord("Win").key == nil)
        #expect(try WindowsChord("ctrl+alt+del").description == "ctrl+alt+delete")
        #expect(throws: ChordNotationError.self) { try MacChord("cmd+") }
        #expect(throws: ChordNotationError.self) { try MacChord("cmd") }
        #expect(throws: ChordNotationError.self) { try WindowsChord("ctrl+*") }
    }

    @Test(arguments: [
        ("com.apple.keylayout.German", 0x0407), ("com.apple.keylayout.Austrian", 0x0407),
        ("com.apple.keylayout.SwissGerman", 0x0807), ("com.apple.keylayout.US", 0x0409),
        ("com.apple.keylayout.ABC", 0x0409), ("com.apple.keylayout.British", 0x0809),
        ("com.apple.keylayout.French", 0x040C), ("com.apple.keylayout.Dutch", 0x0413),
        ("com.apple.keylayout.Spanish-ISO", 0x040A), ("com.apple.keylayout.Italian-Pro", 0x0410),
        ("com.apple.keylayout.Swedish-Pro", 0x041D), ("com.apple.keylayout.Norwegian", 0x0414),
        ("com.apple.keylayout.Danish", 0x0406), ("com.apple.keylayout.Finnish", 0x040B),
        ("com.apple.keylayout.PolishPro", 0x0415), ("com.apple.keylayout.Czech", 0x0405),
        ("com.apple.keylayout.Russian", 0x0419), ("com.apple.keylayout.USInternational-PC", 0x0002_0409),
        ("com.apple.keyboardlayout.roman.keylayout.LogitechGerman", 0x0407),
    ])
    func windowsLayoutForMacInputSource(id: String, klid: UInt32) {
        #expect(WindowsKeyboardLayoutID(inputSourceID: id) == WindowsKeyboardLayoutID(klid))
    }

    @Test func unknownInputSourcesFallBackToLanguageThenUS() {
        #expect(WindowsKeyboardLayoutID(inputSourceID: "org.example.keylayout.Custom", languages: ["de-AT"]) == .german)
        #expect(WindowsKeyboardLayoutID(inputSourceID: "org.example.keylayout.Custom") == .usEnglish)
    }

    @Test func klidNotation() {
        #expect(WindowsKeyboardLayoutID(0x0001_0409).description == "00010409")
        #expect(WindowsKeyboardLayoutID(notation: "0x807") == WindowsKeyboardLayoutID(0x0807))
        #expect(WindowsKeyboardLayoutID(notation: "00000407") == .german)
        #expect(WindowsKeyboardLayoutID(notation: "123456789") == nil)
    }

    @Test func macLayoutForWindowsLayout() {
        #expect(WindowsKeyboardLayoutID.german.macInputSourceID == "com.apple.keylayout.German")
        #expect(WindowsKeyboardLayoutID.usEnglish.macInputSourceID == "com.apple.keylayout.US")
        #expect(WindowsKeyboardLayoutID(0x0807).macInputSourceID == "com.apple.keylayout.SwissGerman")
        #expect(WindowsKeyboardLayoutID(0x0809).macInputSourceID == "com.apple.keylayout.British")
        #expect(WindowsKeyboardLayoutID(0x0001_0409).macInputSourceID == "com.apple.keylayout.Dvorak")
        #expect(WindowsKeyboardLayoutID(0x0000_1234).macInputSourceID == nil)
        #expect(WindowsKeyboardLayoutID.usEnglish.typicalKeyboardType == .ansi)
        #expect(WindowsKeyboardLayoutID(0x0002_0409).typicalKeyboardType == .ansi)
        #expect(WindowsKeyboardLayoutID.german.typicalKeyboardType == .iso)
        #expect(WindowsKeyboardLayoutID(0x0411).typicalKeyboardType == .jis)
    }

    @Test func layoutLoadedForAWindowsLayoutRoundTrips() {
        #expect(Layouts.forWindowsGerman.inputSourceID == "com.apple.keylayout.German")
        #expect(Layouts.forWindowsGerman.keyboardType == .iso)
        #expect(Layouts.forWindowsGerman.windowsLayoutID == .german)
    }

    @Test func realInputSourceKnowsItsWindowsLayout() {
        #expect(Layouts.german.windowsLayoutID == .german)
        #expect(Layouts.swissGerman.windowsLayoutID == WindowsKeyboardLayoutID(0x0807))
        #expect(Layouts.german.keyboardType == .iso)
        #expect(Layouts.us.keyboardType == .ansi)
    }

    @Test func eventFlagsConvertWithSides() {
        // NSEvent.ModifierFlags raw values: generic bit + NX_DEVICE*KEYMASK bit.
        #expect(ModifierFlags(eventFlags: 0x100000 | 0x08) == .leftCommand)
        #expect(ModifierFlags(eventFlags: 0x100000 | 0x10) == .rightCommand)
        #expect(ModifierFlags(eventFlags: 0x80000 | 0x40) == .rightOption)
        #expect(ModifierFlags(eventFlags: 0x40000 | 0x2000) == .rightControl)
        #expect(ModifierFlags(eventFlags: 0x20000 | 0x02 | 0x04) == [.leftShift, .rightShift])
        #expect(ModifierFlags(eventFlags: 0x80000) == .leftOption) // synthesized event without side bits
        #expect(ModifierFlags(eventFlags: 0x08) == []) // stale side bit without the generic flag
        #expect(ModifierFlags(eventFlags: 0x10000 | 0x800000) == [.capsLock, .function])
    }
}
