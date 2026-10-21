@testable import AgentControl
import Foundation
import Testing

struct ScreenMappingTests {
    @Test func agentDesktopsAreOneToOne() throws {
        let mapping = ScreenMapping(desktopWidth: 1280, desktopHeight: 800)
        #expect(mapping.scale == 1)
        #expect(mapping.screenshotWidth == 1280 && mapping.screenshotHeight == 800)
        #expect(try mapping.desktopPoint(PixelPoint(x: 640, y: 400)) == (640, 400))
        #expect(try mapping.desktopPoint(PixelPoint(x: 1279.4, y: 799)) == (1279, 799))
        #expect(mapping.summary.contains("scale 1"))
    }

    @Test func largeDesktopsAreScaledDownAndMappedBack() throws {
        let retina = ScreenMapping(desktopWidth: 2560, desktopHeight: 1600)
        #expect(retina.scale == 0.5)
        #expect(retina.screenshotWidth == 1280 && retina.screenshotHeight == 800)
        #expect(try retina.desktopPoint(PixelPoint(x: 100, y: 50)) == (200, 100))
        #expect(try retina.desktopRect(PixelRect(x: 10, y: 10, width: 100, height: 50))
            == DesktopRect(x: 20, y: 20, width: 200, height: 100))
        #expect(retina.screenshotRect(PixelRect(x: 200, y: 100, width: 40, height: 20))
            == PixelRect(x: 100, y: 50, width: 20, height: 10))

        let fullHD = ScreenMapping(desktopWidth: 1920, desktopHeight: 1080)
        #expect(fullHD.screenshotWidth == 1280 && fullHD.screenshotHeight == 720)
        #expect(try fullHD.desktopPoint(PixelPoint(x: 1279, y: 719)) == (1918, 1078))
        #expect(fullHD.summary.contains("desktop 1920×1080"))

        let portrait = ScreenMapping(desktopWidth: 1200, desktopHeight: 1920)
        #expect(portrait.screenshotWidth == 800 && portrait.screenshotHeight == 1280)
    }

    @Test func pointsOutsideTheScreenAreRefused() throws {
        let mapping = ScreenMapping(desktopWidth: 1280, desktopHeight: 800)
        #expect(throws: ToolArgumentError.self) { try mapping.desktopPoint(PixelPoint(x: -1, y: 10)) }
        #expect(throws: ToolArgumentError.self) { try mapping.desktopPoint(PixelPoint(x: 10, y: 801)) }
        #expect(throws: ToolArgumentError.self) {
            try mapping.desktopRect(PixelRect(x: 1300, y: 0, width: 10, height: 10))
        }
        // Regions reaching over the edge are clipped.
        #expect(try mapping.desktopRect(PixelRect(x: 1200, y: 700, width: 500, height: 500))
            == DesktopRect(x: 1200, y: 700, width: 80, height: 100))
    }
}

struct FrameDiffTests {
    private func frame(width: Int, height: Int, fill: UInt32 = 0xFF00_0000) -> [UInt32] {
        Array(repeating: fill, count: width * height)
    }

    private func bounds(_ old: [UInt32], _ new: [UInt32], width: Int, height: Int) -> DesktopRect? {
        old.withUnsafeBytes { oldBytes in
            new.withUnsafeBytes { newBytes in
                FrameDiff.changedBounds(oldBytes, newBytes, width: width, height: height, stride: width * 4)
            }
        }
    }

    @Test func equalFramesHaveNoChange() {
        let a = frame(width: 64, height: 32)
        #expect(bounds(a, a, width: 64, height: 32) == nil)
        #expect(!FrameDiff.isSignificant(nil))
    }

    @Test func aCaretBlinkIsNotSignificant() {
        let a = frame(width: 64, height: 32)
        var b = a
        for y in 5..<25 { b[y * 64 + 10] = 0xFFFF_FFFF }
        let changed = bounds(a, b, width: 64, height: 32)
        #expect(changed == DesktopRect(x: 10, y: 5, width: 1, height: 20))
        #expect(!FrameDiff.isSignificant(changed))
    }

    @Test func aWindowIsSignificant() {
        let a = frame(width: 64, height: 32)
        var b = a
        for y in 2..<12 { for x in 3..<40 { b[y * 64 + x] = 0xFFFF_FFFF } }
        let changed = bounds(a, b, width: 64, height: 32)
        #expect(changed == DesktopRect(x: 3, y: 2, width: 37, height: 10))
        #expect(FrameDiff.isSignificant(changed))
    }

    @Test func settlesAfterQuietTime() {
        var tracker = SettleTracker(settle: 0.5, start: 0)
        let polls: [(changed: Bool, at: Double)] = [(true, 0.1), (false, 0.4), (true, 0.5), (false, 0.9), (false, 1.0)]
        let settled = polls.map { tracker.update(changed: $0.changed, at: $0.at) }
        #expect(settled == [false, false, false, false, true])
    }
}

struct TextMatchingTests {
    private func matched(_ query: String, _ lines: [String]) -> [String] {
        TextMatching.find(query, in: lines).map { String(lines[$0.lineIndex][$0.range]) }
    }

    @Test func findsCaseAndAccentInsensitively() {
        #expect(matched("grüsse", ["Viele Grüsse"]) == ["Grüsse"])
        #expect(matched("Datei", ["Bearbeiten", "Datei  Bearbeiten  Ansicht"]) == ["Datei"])
        #expect(matched("GRÜSSE", ["grüsse"]) == ["grüsse"])
        #expect(matched("Ubersicht", ["Übersicht"]) == ["Übersicht"])
    }

    @Test func exactMatchesBeatApproximateOnes() {
        let matches = TextMatching.find("Speichern", in: ["Speichem unter", "Speichern"])
        #expect(matches.map(\.lineIndex) == [1])
        #expect(matches.first?.errors == 0)
    }

    @Test func toleratesARecognitionError() {
        // OCR often reads "rn" as "m".
        let lines = ["Nicht speichem", "Abbrechen"]
        #expect(matched("Nicht speichern", lines) == ["Nicht speichem"])
        #expect(TextMatching.find("Nicht speichern", in: lines).first?.errors == 2)
        #expect(matched("Hilfe", lines).isEmpty)
    }

    @Test func shortQueriesMustMatchExactly() {
        #expect(matched("OK", ["0K", "Abbrechen"]).isEmpty)
        #expect(matched("OK", ["Ok"]) == ["Ok"])
    }

    @Test func findsEveryOccurrence() {
        let matches = TextMatching.find("ab", in: ["ab cd ab", "xab"])
        #expect(matches.count == 3)
        #expect(matches.map(\.lineIndex) == [0, 0, 1])
    }

    @Test func rangesPointIntoTheOriginalText() {
        let line = "Grüße ÄÖÜ ß € @ {} \\ |"
        let match = TextMatching.find("äöü ß", in: [line]).first
        #expect(match.map { String(line[$0.range]) } == "ÄÖÜ ß")
    }
}

struct AuditEntryTests {
    @Test func typedTextIsLoggedByLengthOnly() {
        let entry = AuditEntry(
            date: Date(timeIntervalSince1970: 1_790_000_000), client: "claude-code 2.1.306",
            connection: "Windows 11 (10.211.55.9)", tool: "type_text",
            arguments: ["session": "s1", "text": "geheim123"], error: nil, duration: .milliseconds(42))
        let line = entry.line(timeZone: TimeZone(identifier: "Europe/Berlin")!)
        #expect(line.hasPrefix("2026-09-21T16:13:20.000+02:00 client=\"claude-code 2.1.306\""))
        #expect(line.contains(#"connection="Windows 11 (10.211.55.9)" tool=type_text"#))
        #expect(line.contains(#""text":"<9 characters>""#))
        #expect(!line.contains("geheim"))
        #expect(line.hasSuffix(" ok 42ms"))
    }

    @Test func errorsAreQuoted() {
        let entry = AuditEntry(
            date: Date(timeIntervalSince1970: 0), client: "x", connection: nil, tool: "connect",
            arguments: ["connection": "Lab"], error: "The user denied access to \"Lab\".", duration: .seconds(2))
        let line = entry.line(timeZone: TimeZone(identifier: "UTC")!)
        #expect(line == #"1970-01-01T00:00:00.000Z client="x" tool=connect args={"connection":"Lab"} error="The user denied access to \"Lab\"." 2000ms"#)
    }
}

struct ToolArgumentsTests {
    @Test func typedAccessWithReadableErrors() throws {
        let arguments = ToolArguments(["session": "s1", "x": 10.6, "count": 2, "show": "true", "bad": "x"])
        #expect(try arguments.string("session") == "s1")
        #expect(try arguments.integer("count", in: 1...3) == 2)
        #expect(try arguments.integer("x", in: 0...100) == 11)
        #expect(try arguments.integer("missing", in: 1...3, default: 1) == 1)
        #expect(try arguments.bool("show", default: false))
        #expect(try arguments.choice("button", of: ["left", "right"], default: "left") == "left")
        #expect(throws: ToolArgumentError("'count' must be between 1 and 1.")) { try arguments.integer("count", in: 1...1) }
        #expect(throws: ToolArgumentError("Missing argument 'text'.")) { try arguments.string("text") }
        #expect(throws: ToolArgumentError("'bad' must be a number.")) { try arguments.number("bad") }
    }

    @Test func pointsInBothShapes() throws {
        let arguments = ToolArguments(["a": [[1, 2], [3.5, 4]], "b": [["x": 1, "y": 2]], "c": [[1]]])
        #expect(try arguments.points("a") == [PixelPoint(x: 1, y: 2), PixelPoint(x: 3.5, y: 4)])
        #expect(try arguments.points("b") == [PixelPoint(x: 1, y: 2)])
        #expect(throws: ToolArgumentError.self) { try arguments.points("c") }
    }

    @Test func regions() throws {
        let arguments = ToolArguments(["r": ["x": 1, "y": 2, "width": 30, "height": 40], "z": ["x": 1, "y": 2, "width": 0, "height": 4]])
        #expect(try arguments.optionalRect("r") == PixelRect(x: 1, y: 2, width: 30, height: 40))
        #expect(try arguments.optionalRect("none") == nil)
        #expect(throws: ToolArgumentError.self) { try arguments.optionalRect("z") }
    }
}
