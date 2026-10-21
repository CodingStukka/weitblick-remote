@testable import AgentControl
import KeyboardEngine
import KeyboardEngineCarbon
import Testing

/// Real Apple layouts, loaded once: Text Input Sources aborts when two threads call it at once.
enum Layouts {
    static var german: UCKeyTranslateLayoutProvider { all.german }
    static var us: UCKeyTranslateLayoutProvider { all.us }

    private static let all = (
        german: UCKeyTranslateLayoutProvider(windowsLayout: .german)!,
        us: UCKeyTranslateLayoutProvider(windowsLayout: .usEnglish)!
    )
}

struct KeyComboTests {
    private func press(_ keys: String, layout: UCKeyTranslateLayoutProvider, repeat count: Int = 1) throws -> [RDPKeyAction] {
        var engine = KeyboardEngine(layout: layout)
        return try KeyCombo.actions(for: KeyCombo.parse(keys), repeat: count, engine: &engine)
    }

    private func down(_ code: UInt8, extended: Bool = false) -> RDPKeyAction {
        .scancode(code: code, extended: extended, down: true)
    }

    private func up(_ code: UInt8, extended: Bool = false) -> RDPKeyAction {
        .scancode(code: code, extended: extended, down: false)
    }

    @Test func parsesCommonNotations() throws {
        #expect(try KeyCombo.parse("ctrl+shift+esc").map(\.description) == ["ctrl+shift+escape"])
        #expect(try KeyCombo.parse("Win+R").map(\.description) == ["win+r"])
        #expect(try KeyCombo.parse("alt+F4").map(\.description) == ["alt+f4"])
        #expect(try KeyCombo.parse("Enter").map(\.description) == ["return"])
        #expect(try KeyCombo.parse("super+e").map(\.description) == ["win+e"])
        #expect(try KeyCombo.parse("ctrl+a  ctrl+c").map(\.description) == ["ctrl+a", "ctrl+c"])
        #expect(try KeyCombo.parse("pgdn arrowleft PageUp").map(\.description) == ["pagedown", "left", "pageup"])
        #expect(try KeyCombo.parse("ctrl++ ctrl+minus").map(\.description) == ["ctrl+plus", "ctrl+-"])
        #expect(try KeyCombo.parse("win").map(\.description) == ["win"])
    }

    @Test func refusesMacAndUnknownKeys() {
        #expect(throws: ToolArgumentError.self) { try KeyCombo.parse("cmd+c") }
        #expect(throws: ToolArgumentError.self) { try KeyCombo.parse("ctrl+banana") }
        #expect(throws: ToolArgumentError.self) { try KeyCombo.parse("ctrl+") }
        #expect(throws: ToolArgumentError.self) { try KeyCombo.parse("   ") }
        #expect(throws: ToolArgumentError.self) { try KeyCombo.parse("hyper+a") }
    }

    @Test func ctrlZPressesTheKeyThatTypesZ() throws {
        // German: z is on the physical Y key (scancode 0x15); US: on 0x2C.
        #expect(try press("ctrl+z", layout: Layouts.german) == [down(0x1D), down(0x15), up(0x15), up(0x1D)])
        #expect(try press("ctrl+z", layout: Layouts.us) == [down(0x1D), down(0x2C), up(0x2C), up(0x1D)])
        #expect(try press("ctrl+y", layout: Layouts.german) == [down(0x1D), down(0x2C), up(0x2C), up(0x1D)])
    }

    @Test func plusOnGermanIsTheKeyRightOfU() throws {
        #expect(try press("ctrl++", layout: Layouts.german) == [down(0x1D), down(0x1B), up(0x1B), up(0x1D)])
    }

    @Test func windowsAndFunctionKeys() throws {
        #expect(try press("win+r", layout: Layouts.german)
            == [down(0x5B, extended: true), down(0x13), up(0x13), up(0x5B, extended: true)])
        #expect(try press("alt+f4", layout: Layouts.german) == [down(0x38), down(0x3E), up(0x3E), up(0x38)])
        #expect(try press("enter", layout: Layouts.german) == [down(0x1C), up(0x1C)])
        #expect(try press("ctrl+shift+esc", layout: Layouts.german)
            == [down(0x1D), down(0x2A), down(0x01), up(0x01), up(0x2A), up(0x1D)])
    }

    @Test func sequencesAndRepeats() throws {
        #expect(try press("ctrl+a ctrl+c", layout: Layouts.german)
            == [down(0x1D), down(0x1E), up(0x1E), up(0x1D), down(0x1D), down(0x2E), up(0x2E), up(0x1D)])
        #expect(try press("tab", layout: Layouts.us, repeat: 3) == Array(repeating: [down(0x0F), up(0x0F)], count: 3).flatMap { $0 })
    }

    @Test func charactersWithoutAKeyAreRefused() {
        // '@' needs AltGr on German: there is no unmodified key for it.
        #expect(throws: ToolArgumentError.self) { try press("ctrl+@", layout: Layouts.german) }
    }
}

struct TextTypingTests {
    private func units(_ groups: [[RDPKeyAction]]) -> [String] {
        groups.map { group in
            group.compactMap { action -> String? in
                switch action {
                case .unicode(let unit, true): String(format: "U+%04X", unit)
                case .scancode(let code, _, true): String(format: "SC%02X", code)
                default: nil
                }
            }.joined(separator: " ")
        }
    }

    @Test func textIsUnicodeAndLineBreaksAreEnter() {
        #expect(units(TextTyping.actions(for: "aß\n€")) == ["U+0061", "U+00DF", "SC1C", "U+20AC"])
        #expect(units(TextTyping.actions(for: "a\r\nb\tc")) == ["U+0061", "SC1C", "U+0062", "SC0F", "U+0063"])
        #expect(units(TextTyping.actions(for: "x\r")) == ["U+0078", "SC1C"])
    }

    @Test func everyDownHasItsUp() {
        for group in TextTyping.actions(for: "Grüße ÄÖÜ ß € @ {} \\ | 😀\n") {
            #expect(group.count % 2 == 0)
            for pair in stride(from: 0, to: group.count, by: 2) {
                switch (group[pair], group[pair + 1]) {
                case (.unicode(let a, true), .unicode(let b, false)): #expect(a == b)
                case (.scancode(let a, _, true), .scancode(let b, _, false)): #expect(a == b)
                default: Issue.record("unpaired \(group)")
                }
            }
        }
    }

    @Test func emojiIsASurrogatePair() {
        #expect(units(TextTyping.actions(for: "😀")) == ["U+D83D U+DE00"])
    }

    @Test func controlCharactersAreDropped() {
        #expect(TextTyping.actions(for: "\u{07}\u{1B}").isEmpty)
    }

    @Test func clickModifiers() throws {
        let held = try HeldModifiers.parse(["ctrl", "Shift", "ctrl"])
        #expect(HeldModifiers.press(held) == [.scancode(code: 0x1D, extended: false, down: true), .scancode(code: 0x2A, extended: false, down: true)])
        #expect(HeldModifiers.release(held) == [.scancode(code: 0x2A, extended: false, down: false), .scancode(code: 0x1D, extended: false, down: false)])
        #expect(throws: ToolArgumentError.self) { try HeldModifiers.parse(["cmd"]) }
    }
}
