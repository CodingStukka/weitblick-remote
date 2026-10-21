import KeyboardEngine

/// Text as RDP keyboard input: Unicode events (independent of the keyboard layout), except line
/// breaks and tabs, which press Enter and Tab so dialogs and editors react to them like to keys.
public enum TextTyping {
    static let enter = Scancode(0x1C)
    static let tab = Scancode(0x0F)

    /// Actions grouped per character, so the caller can pace long texts between groups.
    public static func actions(for text: String) -> [[RDPKeyAction]] {
        var groups: [[RDPKeyAction]] = []
        var previousWasCarriageReturn = false
        for scalar in text.unicodeScalars {
            switch scalar {
            case "\r":
                groups.append(tap(enter))
                previousWasCarriageReturn = true
                continue
            case "\n":
                // "\r\n" is one line break.
                if !previousWasCarriageReturn { groups.append(tap(enter)) }
            case "\t":
                groups.append(tap(tab))
            default:
                // Other control characters would type nothing useful.
                if scalar.value < 0x20 || scalar.value == 0x7F { break }
                groups.append(String(scalar).utf16.flatMap { unit in
                    [RDPKeyAction.unicode(unit, down: true), .unicode(unit, down: false)]
                })
            }
            previousWasCarriageReturn = false
        }
        return groups
    }

    private static func tap(_ scancode: Scancode) -> [RDPKeyAction] {
        [
            .scancode(code: scancode.code, extended: scancode.extended, down: true),
            .scancode(code: scancode.code, extended: scancode.extended, down: false),
        ]
    }
}

/// Windows modifier keys held around a click (`modifiers: ["ctrl", "shift"]`).
public enum HeldModifiers {
    public static let names = ["ctrl", "shift", "alt", "win"]

    public static func parse(_ requested: [String]) throws(ToolArgumentError) -> [Scancode] {
        var scancodes: [Scancode] = []
        for name in requested {
            let scancode: Scancode
            switch name.lowercased() {
            case "ctrl", "control": scancode = Scancode(0x1D)
            case "shift": scancode = Scancode(0x2A)
            case "alt": scancode = Scancode(0x38)
            case "win", "windows", "super", "meta": scancode = Scancode(0x5B, extended: true)
            default:
                throw ToolArgumentError("Unknown modifier '\(name)'; use \(names.joined(separator: ", ")).")
            }
            if !scancodes.contains(scancode) { scancodes.append(scancode) }
        }
        return scancodes
    }

    public static func press(_ scancodes: [Scancode]) -> [RDPKeyAction] {
        scancodes.map { .scancode(code: $0.code, extended: $0.extended, down: true) }
    }

    public static func release(_ scancodes: [Scancode]) -> [RDPKeyAction] {
        scancodes.reversed().map { .scancode(code: $0.code, extended: $0.extended, down: false) }
    }
}
