@testable import KeyboardEngine
import KeyboardEngineCarbon

/// Real Apple layouts. Keyboard type pinned so results don't depend on the test Mac. All are loaded in
/// one static initializer: Text Input Sources aborts when two threads call it at once.
enum Layouts {
    static var german: UCKeyTranslateLayoutProvider { all.german }
    static var us: UCKeyTranslateLayoutProvider { all.us }
    static var usOnISO: UCKeyTranslateLayoutProvider { all.usOnISO }
    static var swissGerman: UCKeyTranslateLayoutProvider { all.swissGerman }
    static var russian: UCKeyTranslateLayoutProvider { all.russian }
    static var osage: UCKeyTranslateLayoutProvider { all.osage }
    /// Loaded through `init(windowsLayout:)` for KLID 00000407.
    static var forWindowsGerman: UCKeyTranslateLayoutProvider { all.forWindowsGerman }

    private static let all = (
        german: load("com.apple.keylayout.German", keyboardTypeCode: 41),
        us: load("com.apple.keylayout.US", keyboardTypeCode: 40),
        usOnISO: load("com.apple.keylayout.US", keyboardTypeCode: 41),
        swissGerman: load("com.apple.keylayout.SwissGerman", keyboardTypeCode: 41),
        russian: load("com.apple.keylayout.Russian", keyboardTypeCode: 41),
        osage: load("com.apple.keylayout.Osage-QWERTY", keyboardTypeCode: 40),
        forWindowsGerman: UCKeyTranslateLayoutProvider(windowsLayout: .german)!
    )

    private static func load(_ id: String, keyboardTypeCode: UInt32) -> UCKeyTranslateLayoutProvider {
        guard let layout = UCKeyTranslateLayoutProvider(inputSourceID: id, keyboardTypeCode: keyboardTypeCode) else {
            fatalError("Keyboard layout \(id) is not installed")
        }
        return layout
    }
}

/// macOS virtual key codes (kVK_* from HIToolbox/Events.h).
enum K {
    static let a: UInt16 = 0x00, s: UInt16 = 0x01, d: UInt16 = 0x02, f: UInt16 = 0x03
    static let e: UInt16 = 0x0E, l: UInt16 = 0x25, n: UInt16 = 0x2D, o: UInt16 = 0x1F, u: UInt16 = 0x20
    static let c: UInt16 = 0x08, v: UInt16 = 0x09, x: UInt16 = 0x07, q: UInt16 = 0x0C, t: UInt16 = 0x11
    /// kVK_ANSI_Z / kVK_ANSI_Y: named after the US legend (QWERTZ swaps them).
    static let ansiZ: UInt16 = 0x06, ansiY: UInt16 = 0x10
    static let one: UInt16 = 0x12, two: UInt16 = 0x13, five: UInt16 = 0x17, six: UInt16 = 0x16, seven: UInt16 = 0x1A
    static let eight: UInt16 = 0x1C, nine: UInt16 = 0x19, zero: UInt16 = 0x1D
    static let equal: UInt16 = 0x18, comma: UInt16 = 0x2B
    static let leftBracket: UInt16 = 0x21, rightBracket: UInt16 = 0x1E, slash: UInt16 = 0x2C
    static let isoSection: UInt16 = 0x0A, grave: UInt16 = 0x32
    static let space: UInt16 = 0x31, tab: UInt16 = 0x30, returnKey: UInt16 = 0x24
    static let backspace: UInt16 = 0x33, escape: UInt16 = 0x35, forwardDelete: UInt16 = 0x75
    static let left: UInt16 = 0x7B, right: UInt16 = 0x7C, down: UInt16 = 0x7D, up: UInt16 = 0x7E
    static let home: UInt16 = 0x73, end: UInt16 = 0x77, pageUp: UInt16 = 0x74, pageDown: UInt16 = 0x79
    static let help: UInt16 = 0x72
    static let f1: UInt16 = 0x7A, f4: UInt16 = 0x76, f12: UInt16 = 0x6F
    static let f13: UInt16 = 0x69, f14: UInt16 = 0x6B, f15: UInt16 = 0x71, f16: UInt16 = 0x6A
    static let f17: UInt16 = 0x40, f18: UInt16 = 0x4F, f19: UInt16 = 0x50
    static let keypad1: UInt16 = 0x53, keypadEnter: UInt16 = 0x4C, keypadDivide: UInt16 = 0x4B
    static let keypadClear: UInt16 = 0x47, keypadEquals: UInt16 = 0x51
    static let jisYen: UInt16 = 0x5D, jisUnderscore: UInt16 = 0x5E, jisKana: UInt16 = 0x68, jisEisu: UInt16 = 0x66
    static let capsLock: UInt16 = 0x39
}

/// Set-1 scancodes, from Microsoft's keyboard scan code specification.
enum SC {
    static let lCtrl = Scancode(0x1D), rCtrl = Scancode(0x1D, extended: true)
    static let lShift = Scancode(0x2A), rShift = Scancode(0x36)
    static let lAlt = Scancode(0x38), rAlt = Scancode(0x38, extended: true)
    static let lWin = Scancode(0x5B, extended: true), rWin = Scancode(0x5C, extended: true)
    static let capsLock = Scancode(0x3A), numLock = Scancode(0x45), scrollLock = Scancode(0x46)
    static let a = Scancode(0x1E), c = Scancode(0x2E), d = Scancode(0x20), e = Scancode(0x12)
    static let f = Scancode(0x21), l = Scancode(0x26), n = Scancode(0x31), q = Scancode(0x10)
    static let v = Scancode(0x2F), x = Scancode(0x2D), o = Scancode(0x18)
    /// Physical US-Y / US-Z positions.
    static let usY = Scancode(0x15), usZ = Scancode(0x2C)
    static let one = Scancode(0x02), five = Scancode(0x06), seven = Scancode(0x08)
    static let grave = Scancode(0x29), oem102 = Scancode(0x56)
    static let space = Scancode(0x39), tab = Scancode(0x0F), enter = Scancode(0x1C)
    static let backspace = Scancode(0x0E), escape = Scancode(0x01)
    static let delete = Scancode(0x53, extended: true), insert = Scancode(0x52, extended: true)
    static let home = Scancode(0x47, extended: true), end = Scancode(0x4F, extended: true)
    static let left = Scancode(0x4B, extended: true), right = Scancode(0x4D, extended: true)
    static let up = Scancode(0x48, extended: true), down = Scancode(0x50, extended: true)
    static let f1 = Scancode(0x3B), f4 = Scancode(0x3E), f12 = Scancode(0x58)
    static let f16 = Scancode(0x67), f17 = Scancode(0x68), f18 = Scancode(0x69), f19 = Scancode(0x6A)
    static let printScreen = Scancode(0x37, extended: true), breakKey = Scancode(0x46, extended: true)
    static let keypad1 = Scancode(0x4F), keypadEnter = Scancode(0x1C, extended: true)
    static let keypadDivide = Scancode(0x35, extended: true)
}

func down(_ scancode: Scancode) -> RDPKeyAction { .scancode(code: scancode.code, extended: scancode.extended, down: true) }
func up(_ scancode: Scancode) -> RDPKeyAction { .scancode(code: scancode.code, extended: scancode.extended, down: false) }
func tap(_ scancode: Scancode) -> [RDPKeyAction] { [down(scancode), up(scancode)] }
func text(_ string: String) -> [RDPKeyAction] {
    string.utf16.flatMap { [RDPKeyAction.unicode($0, down: true), .unicode($0, down: false)] }
}

/// The eight modifier keys with their kVK codes.
enum Mod: CaseIterable {
    case lCmd, rCmd, lOpt, rOpt, lCtrl, rCtrl, lShift, rShift

    var keyCode: UInt16 {
        switch self {
        case .lCmd: 0x37
        case .rCmd: 0x36
        case .lOpt: 0x3A
        case .rOpt: 0x3D
        case .lCtrl: 0x3B
        case .rCtrl: 0x3E
        case .lShift: 0x38
        case .rShift: 0x3C
        }
    }

    var flag: ModifierFlags {
        switch self {
        case .lCmd: .leftCommand
        case .rCmd: .rightCommand
        case .lOpt: .leftOption
        case .rOpt: .rightOption
        case .lCtrl: .leftControl
        case .rCtrl: .rightControl
        case .lShift: .leftShift
        case .rShift: .rightShift
        }
    }
}

/// Plays a physical Mac keyboard into an engine, the way AppKit delivers events.
struct Harness {
    var engine: KeyboardEngine
    var flags: ModifierFlags = []
    var time: Double = 1000
    var keyboardType: PhysicalKeyboardType?
    private(set) var pending: [RDPKeyAction] = []
    private(set) var lastPassedToApp = false

    init(_ layout: any KeyboardLayoutProvider = Layouts.german, config: KeyboardConfig = KeyboardConfig()) {
        engine = KeyboardEngine(config: config, layout: layout)
        _ = engine.focusGained(modifiers: [])
    }

    /// Actions since the last call.
    mutating func take() -> [RDPKeyAction] {
        defer { pending = [] }
        return pending
    }

    mutating func press(_ modifier: Mod) {
        flags.insert(modifier.flag)
        send(KeyEvent(kind: .flagsChanged, keyCode: modifier.keyCode, modifiers: flags, timestamp: tick()))
    }

    mutating func release(_ modifier: Mod) {
        flags.remove(modifier.flag)
        send(KeyEvent(kind: .flagsChanged, keyCode: modifier.keyCode, modifiers: flags, timestamp: tick()))
    }

    mutating func down(_ keyCode: UInt16, isRepeat: Bool = false) {
        send(KeyEvent(kind: .down, keyCode: keyCode, modifiers: flags, isRepeat: isRepeat, timestamp: tick(), keyboardType: keyboardType))
    }

    mutating func up(_ keyCode: UInt16) {
        send(KeyEvent(kind: .up, keyCode: keyCode, modifiers: flags, timestamp: tick(), keyboardType: keyboardType))
    }

    mutating func type(_ keyCode: UInt16) {
        down(keyCode)
        up(keyCode)
    }

    /// Presses `modifiers` (in order), types the key, releases them in reverse.
    mutating func chord(_ modifiers: [Mod], _ keyCode: UInt16) {
        modifiers.forEach { press($0) }
        type(keyCode)
        modifiers.reversed().forEach { release($0) }
    }

    mutating func toggleCapsLock() {
        if flags.contains(.capsLock) { flags.remove(.capsLock) } else { flags.insert(.capsLock) }
        send(KeyEvent(kind: .flagsChanged, keyCode: K.capsLock, modifiers: flags, timestamp: tick()))
    }

    mutating func wait(_ seconds: Double) { time += seconds }

    mutating func pointer() { pending += engine.prepareForPointerEvent() }
    mutating func focusLost() { pending += engine.focusLost() }
    mutating func focusGained() { pending += engine.focusGained(modifiers: flags) }

    mutating func send(_ event: KeyEvent) {
        let result = engine.handle(event)
        pending += result.actions
        lastPassedToApp = result.passToApp
    }

    private mutating func tick() -> Double {
        time += 0.05
        return time
    }
}

/// The remote keyboard as Windows would see it; records protocol violations.
struct RemoteSimulator {
    private(set) var held: Set<Scancode> = []
    private(set) var violations: [String] = []
    private(set) var capsLock = false

    mutating func apply(_ actions: [RDPKeyAction]) {
        for action in actions {
            switch action {
            case .scancode(let code, let extended, true):
                let scancode = Scancode(code, extended: extended)
                if held.contains(scancode), RemoteModifier(scancode: scancode) != nil {
                    violations.append("modifier pressed twice: \(scancode)")
                }
                if scancode == SC.capsLock, !held.contains(scancode) { capsLock.toggle() }
                held.insert(scancode)
            case .scancode(let code, let extended, false):
                let scancode = Scancode(code, extended: extended)
                if held.remove(scancode) == nil { violations.append("break without make: \(scancode)") }
            case .sync(let caps, _, _):
                capsLock = caps
            case .unicode, .pause:
                break
            }
        }
    }
}

/// Concatenates action lists (keeps long expectations cheap to type-check).
func sequence(_ parts: [RDPKeyAction]...) -> [RDPKeyAction] { parts.flatMap { $0 } }
