/// One MCP tool as `tools/list` describes it.
public struct ToolDefinition: Equatable, Sendable {
    public var name: String
    public var title: String
    public var description: String
    /// JSON Schema of the arguments object.
    public var inputSchema: JSONValue
    /// Only looks (screenshots, OCR, lists); hint for clients that auto-approve read-only tools.
    public var readOnly: Bool

    public init(name: String, title: String, description: String, inputSchema: JSONValue, readOnly: Bool) {
        self.name = name
        self.title = title
        self.description = description
        self.inputSchema = inputSchema
        self.readOnly = readOnly
    }

    var json: JSONValue {
        [
            "name": .string(name),
            "title": .string(title),
            "description": .string(description),
            "inputSchema": inputSchema,
            "annotations": [
                "title": .string(title),
                "readOnlyHint": .bool(readOnly),
                "destructiveHint": .bool(!readOnly),
                "openWorldHint": true,
            ],
        ]
    }
}

/// The tools Weitblick Remote offers, their schemas and the server instructions.
public enum ToolCatalog {
    /// Longest screenshot side; larger desktops are scaled down (see `ScreenMapping`).
    public static let maxScreenshotSide = 1280

    public static let instructions = """
        Weitblick Remote controls Windows computers over RDP: screenshots, mouse, keyboard, clipboard \
        and on-screen text. Nothing touches the user's Mac.
        - Start with list_connections, then connect. To work in a session the user already has open, \
        use its id from list_sessions instead (connecting again would log the user out).
        - Take a screenshot first and whenever you are unsure what is on screen.
        - Coordinates are screenshot pixels; the screenshot result states the size and scale.
        - Prefer find_text to locate labels and buttons: it returns click points.
        - Use type_text for text and key for shortcuts (ctrl+s, win+r, alt+f4, enter).
        - Input tools accept screenshot: true and then return a screenshot once the screen has settled.
        - Disconnect when you are done.
        """

    public static let all: [ToolDefinition] = [
        ToolDefinition(
            name: "list_connections", title: "List connections",
            description: "Saved RDP connections with their agent access (ask, always, never) and open sessions.",
            inputSchema: schema([:]), readOnly: true),
        ToolDefinition(
            name: "list_sessions", title: "List sessions",
            description: "Open sessions: the ones you connected and the ones the user has open (use their id to "
                + "attach; the user may be asked for permission).",
            inputSchema: schema([:]), readOnly: true),
        ToolDefinition(
            name: "connect", title: "Connect",
            description: "Opens a session to a saved connection and waits until the desktop is up. Returns the "
                + "session id and desktop size. The user may be asked for permission (up to 2 minutes). The "
                + "session runs without a window unless show is true.",
            inputSchema: schema([
                "connection": string("Connection name or id from list_connections"),
                "width": integer("Desktop width in pixels (800 to 1920, default 1280)", minimum: 800, maximum: 1920),
                "height": integer("Desktop height in pixels (600 to 1200, default 800)", minimum: 600, maximum: 1200),
                "show": boolean("Open a tab the user can watch (without bringing the app to the front)"),
            ], required: ["connection"]), readOnly: false),
        ToolDefinition(
            name: "disconnect", title: "Disconnect",
            description: "Closes a session you connected. For a session of the user it only ends your control.",
            inputSchema: schema(["session": session], required: ["session"]), readOnly: false),
        ToolDefinition(
            name: "screenshot", title: "Screenshot",
            description: "The current screen (PNG, JPEG when large) plus its size, scale and the cursor position.",
            inputSchema: schema(["session": session, "region": region], required: ["session"]), readOnly: true),
        ToolDefinition(
            name: "click", title: "Click",
            description: "Clicks at a point (screenshot pixels).",
            inputSchema: schema([
                "session": session, "x": coordinate("x"), "y": coordinate("y"), "button": button,
                "count": integer("1 = click, 2 = double click, 3 = triple click (default 1)", minimum: 1, maximum: 3),
                "modifiers": modifiers, "screenshot": screenshotFlag,
            ], required: ["session", "x", "y"]), readOnly: false),
        ToolDefinition(
            name: "mouse_move", title: "Move the mouse",
            description: "Moves the pointer (screenshot pixels), e.g. to show a tooltip.",
            inputSchema: schema([
                "session": session, "x": coordinate("x"), "y": coordinate("y"), "screenshot": screenshotFlag,
            ], required: ["session", "x", "y"]), readOnly: false),
        ToolDefinition(
            name: "mouse_down", title: "Press a mouse button",
            description: "Presses a mouse button and keeps it down (at x, y if given). Release it with mouse_up.",
            inputSchema: schema([
                "session": session, "x": coordinate("x"), "y": coordinate("y"), "button": button,
                "screenshot": screenshotFlag,
            ], required: ["session"]), readOnly: false),
        ToolDefinition(
            name: "mouse_up", title: "Release a mouse button",
            description: "Releases a mouse button (at x, y if given).",
            inputSchema: schema([
                "session": session, "x": coordinate("x"), "y": coordinate("y"), "button": button,
                "screenshot": screenshotFlag,
            ], required: ["session"]), readOnly: false),
        ToolDefinition(
            name: "drag", title: "Drag",
            description: "Presses the button at the first point, moves along the path and releases at the last.",
            inputSchema: schema([
                "session": session,
                "path": [
                    "type": "array",
                    "description": "Points [[x, y], ...] in screenshot pixels, at least two",
                    "minItems": 2,
                    "items": ["type": "array", "items": ["type": "number"], "minItems": 2, "maxItems": 2],
                ],
                "button": button, "screenshot": screenshotFlag,
            ], required: ["session", "path"]), readOnly: false),
        ToolDefinition(
            name: "scroll", title: "Scroll",
            description: "Turns the mouse wheel at a point. dy > 0 scrolls down, dx > 0 scrolls right (notches).",
            inputSchema: schema([
                "session": session, "x": coordinate("x"), "y": coordinate("y"),
                "dx": integer("Horizontal notches, positive scrolls right", minimum: -50, maximum: 50),
                "dy": integer("Vertical notches, positive scrolls down", minimum: -50, maximum: 50),
                "screenshot": screenshotFlag,
            ], required: ["session", "x", "y"]), readOnly: false),
        ToolDefinition(
            name: "type_text", title: "Type text",
            description: "Types text as Unicode input, independent of the keyboard layout. \\n presses Enter, "
                + "\\t presses Tab.",
            inputSchema: schema([
                "session": session, "text": string("The text to type"), "screenshot": screenshotFlag,
            ], required: ["session", "text"]), readOnly: false),
        ToolDefinition(
            name: "key", title: "Press keys",
            description: "Presses a Windows key combination such as ctrl+shift+esc, win+r, alt+f4, enter, f5 or "
                + "ctrl+z; several separated by spaces are pressed one after another. Letters and symbols go "
                + "through the session's keyboard layout. Keys: \(KeyCombo.keyNameSummary).",
            inputSchema: schema([
                "session": session, "keys": string("Key combination(s), e.g. ctrl+s or \"ctrl+a ctrl+c\""),
                "repeat": integer("How often to press it (default 1)", minimum: 1, maximum: 100),
                "screenshot": screenshotFlag,
            ], required: ["session", "keys"]), readOnly: false),
        ToolDefinition(
            name: "clipboard_get", title: "Read the clipboard",
            description: "The text on the remote clipboard.",
            inputSchema: schema(["session": session], required: ["session"]), readOnly: true),
        ToolDefinition(
            name: "clipboard_set", title: "Set the clipboard",
            description: "Puts text on the remote clipboard (paste it with key ctrl+v).",
            inputSchema: schema(["session": session, "text": string("The text")], required: ["session", "text"]),
            readOnly: false),
        ToolDefinition(
            name: "wait", title: "Wait",
            description: "Waits a number of seconds.",
            inputSchema: schema([
                "session": session,
                "seconds": number("Seconds to wait (up to 30)", minimum: 0, maximum: 30),
            ], required: ["session", "seconds"]), readOnly: true),
        ToolDefinition(
            name: "wait_for_screen", title: "Wait for the screen",
            description: "Waits until the screen stops changing (a blinking caret does not count).",
            inputSchema: schema([
                "session": session,
                "timeout": number("Seconds to wait at most (default 10, up to 60)", minimum: 0, maximum: 60),
                "settle_ms": integer("How long nothing may change (default 500 ms)", minimum: 100, maximum: 5000),
            ], required: ["session"]), readOnly: true),
        ToolDefinition(
            name: "read_screen", title: "Read the screen",
            description: "Recognizes the text on screen (German and English) with boxes and click points in "
                + "screenshot pixels.",
            inputSchema: schema(["session": session, "region": region], required: ["session"]), readOnly: true),
        ToolDefinition(
            name: "find_text", title: "Find text",
            description: "Finds text on screen (case-insensitive, tolerant of small recognition errors) and returns "
                + "matches with boxes and click points in screenshot pixels, best first.",
            inputSchema: schema([
                "session": session, "query": string("The text to find"), "region": region,
            ], required: ["session", "query"]), readOnly: true),
    ]

    public static func definition(named name: String) -> ToolDefinition? {
        all.first { $0.name == name }
    }

    // MARK: - Schema helpers

    private static func schema(_ properties: [String: JSONValue], required: [String] = []) -> JSONValue {
        var object: [String: JSONValue] = [
            "type": "object", "properties": .object(properties), "additionalProperties": false,
        ]
        if !required.isEmpty { object["required"] = .array(required.map(JSONValue.string)) }
        return .object(object)
    }

    private static func string(_ description: String) -> JSONValue {
        ["type": "string", "description": .string(description)]
    }

    private static func boolean(_ description: String) -> JSONValue {
        ["type": "boolean", "description": .string(description)]
    }

    private static func integer(_ description: String, minimum: Int, maximum: Int) -> JSONValue {
        ["type": "integer", "description": .string(description), "minimum": .int(minimum), "maximum": .int(maximum)]
    }

    private static func number(_ description: String, minimum: Double, maximum: Double) -> JSONValue {
        ["type": "number", "description": .string(description), "minimum": .double(minimum), "maximum": .double(maximum)]
    }

    private static func coordinate(_ axis: String) -> JSONValue {
        ["type": "number", "description": .string("\(axis) in screenshot pixels")]
    }

    private static let session = string("Session id from connect or list_sessions")
    private static let screenshotFlag = boolean("Return a screenshot once the screen has settled")
    private static let button: JSONValue = [
        "type": "string", "enum": ["left", "right", "middle"], "description": "Mouse button (default left)",
    ]
    private static let modifiers: JSONValue = [
        "type": "array", "description": "Keys held during the click",
        "items": ["type": "string", "enum": ["ctrl", "shift", "alt", "win"]],
    ]
    private static let region: JSONValue = [
        "type": "object",
        "description": "Only this part of the screen, in screenshot pixels",
        "properties": [
            "x": ["type": "number"], "y": ["type": "number"],
            "width": ["type": "number"], "height": ["type": "number"],
        ],
        "required": ["x", "y", "width", "height"],
        "additionalProperties": false,
    ]
}
