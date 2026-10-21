import KeyboardEngine

/// The `key` tool's notation: Windows combos like `ctrl+shift+esc`, `win+r`, `alt+f4`, `enter`, several
/// separated by spaces. Accepts the names models commonly use (`super`, `pgup`, `arrowleft`, `ctrl++`)
/// and maps them onto KeyboardEngine's `WindowsChord`, whose characters resolve through the session's
/// keyboard layout when they are pressed.
public enum KeyCombo {
    /// Parses one or more combos separated by whitespace.
    public static func parse(_ notation: String) throws(ToolArgumentError) -> [WindowsChord] {
        let combos = notation.split(whereSeparator: \.isWhitespace).map(String.init)
        guard !combos.isEmpty else { throw ToolArgumentError("'keys' is empty.") }
        var chords: [WindowsChord] = []
        for combo in combos {
            chords.append(try parseCombo(combo))
        }
        return chords
    }

    /// Keyboard actions for pressing `chords` `repeat` times through `engine` (whose layout matches the
    /// session's Windows layout). Throws for keys the layout cannot press.
    public static func actions(
        for chords: [WindowsChord], repeat count: Int = 1, engine: inout KeyboardEngine
    ) throws(ToolArgumentError) -> [RDPKeyAction] {
        var actions: [RDPKeyAction] = []
        for _ in 0..<max(1, count) {
            for chord in chords {
                let tapped = engine.tap(chord)
                if tapped.isEmpty, chord.key != nil {
                    throw ToolArgumentError(
                        "'\(chord)' has no key on the session's keyboard layout. Use type_text for characters.")
                }
                actions += tapped
            }
        }
        return actions
    }

    /// For the tool description.
    public static let keyNameSummary = "ctrl, shift, alt, win; enter, esc, tab, space, backspace, delete, insert, "
        + "home, end, pageup, pagedown, up, down, left, right, f1-f20, printscreen, pause, menu, letters, digits "
        + "and symbols (plus, minus, comma, period, slash)"

    private static func parseCombo(_ combo: String) throws(ToolArgumentError) -> WindowsChord {
        // "ctrl++" and a lone "+" mean the plus key.
        var text = combo
        if text == "+" {
            text = "plus"
        } else if text.hasSuffix("++") {
            text = String(text.dropLast()) + "plus"
        }
        var parts = text.split(separator: "+", omittingEmptySubsequences: false).map(String.init)
        guard !parts.contains(where: \.isEmpty) else {
            throw ToolArgumentError("Invalid key combination '\(combo)': empty part.")
        }
        for index in parts.indices {
            let lowered = parts[index].lowercased()
            if lowered == "cmd" || lowered == "command" || lowered == "⌘" {
                throw ToolArgumentError(
                    "Invalid key combination '\(combo)': Windows has no Cmd key. Use ctrl (copy is ctrl+c) or win.")
            }
            if let alias = aliases[lowered] {
                parts[index] = alias
            } else if parts[index].count == 1 {
                parts[index] = lowered
            }
        }
        do {
            return try WindowsChord(parts.joined(separator: "+"))
        } catch {
            throw ToolArgumentError("Invalid key combination '\(combo)': \(error.reason). Keys: \(keyNameSummary).")
        }
    }

    /// Names models use → KeyboardEngine notation.
    private static let aliases: [String: String] = [
        // Modifiers
        "control": "ctrl", "ctl": "ctrl", "strg": "ctrl", "super": "win", "meta": "win", "windows": "win",
        "start": "win", "lwin": "win", "rwin": "win", "option": "alt", "opt": "alt", "altgr": "ctrl+alt",
        "lctrl": "ctrl", "rctrl": "ctrl", "lshift": "shift", "rshift": "shift", "lalt": "alt", "ralt": "alt",
        // Named keys
        "return": "return", "enter": "return", "escape": "escape", "esc": "escape",
        "spacebar": "space", " ": "space", "bksp": "backspace", "back": "backspace",
        "del": "delete", "ins": "insert",
        "pgup": "pageup", "page_up": "pageup", "pgdn": "pagedown", "pgdown": "pagedown", "page_down": "pagedown",
        "arrowup": "up", "arrow_up": "up", "uparrow": "up",
        "arrowdown": "down", "arrow_down": "down", "downarrow": "down",
        "arrowleft": "left", "arrow_left": "left", "leftarrow": "left",
        "arrowright": "right", "arrow_right": "right", "rightarrow": "right",
        "prtsc": "printscreen", "prtscn": "printscreen", "print": "printscreen", "print_screen": "printscreen",
        "snapshot": "printscreen", "contextmenu": "menu", "context_menu": "menu", "apps": "menu",
        "scroll_lock": "scrolllock", "num_lock": "numlock", "break": "pause",
        // Symbols by name
        "minus": "-", "hyphen": "-", "equal": "=", "equals": "=", "comma": ",", "period": ".", "dot": ".",
        "slash": "/", "backslash": "\\", "semicolon": ";", "quote": "'", "apostrophe": "'",
        "backtick": "`", "grave": "`", "bracketleft": "[", "bracketright": "]",
    ]
}
