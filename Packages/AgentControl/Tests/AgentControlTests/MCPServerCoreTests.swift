@testable import AgentControl
import Testing

struct MCPServerCoreTests {
    private func makeCore() -> MCPServerCore {
        MCPServerCore(serverName: "weitblick-remote", serverVersion: "1.1.0", instructions: ToolCatalog.instructions,
                      tools: ToolCatalog.all)
    }

    private func reply(_ action: MCPServerCore.Action) throws -> JSONValue {
        guard case .reply(let line) = action else {
            Issue.record("expected a reply, got \(action)")
            return .null
        }
        #expect(!line.contains("\n"))
        return try JSONValue.decode(line)
    }

    @Test(arguments: ["2025-06-18", "2025-03-26", "2024-11-05"])
    func initializeEchoesASupportedVersion(version: String) throws {
        var core = makeCore()
        let response = try reply(core.handle(line: """
            {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"\(version)",\
            "capabilities":{},"clientInfo":{"name":"claude-code","version":"2.1.306"}}}
            """))
        #expect(response["id"] == 1)
        #expect(response["result"]?["protocolVersion"]?.stringValue == version)
        #expect(response["result"]?["serverInfo"]?["name"] == "weitblick-remote")
        #expect(response["result"]?["capabilities"]?["tools"] != nil)
        #expect(response["result"]?["instructions"]?.stringValue?.contains("find_text") == true)
        #expect(core.client == MCPClientInfo(name: "claude-code", version: "2.1.306"))
        #expect(core.client.displayName == "claude-code 2.1.306")
    }

    @Test func unknownVersionGetsTheNewest() throws {
        var core = makeCore()
        let response = try reply(core.handle(line: #"{"jsonrpc":"2.0","id":"a","method":"initialize","params":{"protocolVersion":"2099-01-01"}}"#))
        #expect(response["id"] == "a")
        #expect(response["result"]?["protocolVersion"] == "2025-06-18")
        #expect(core.client == .unknown)
    }

    @Test func notificationsGetNoAnswer() {
        var core = makeCore()
        #expect(core.handle(line: #"{"jsonrpc":"2.0","method":"notifications/initialized"}"#) == .none)
        #expect(core.isInitialized)
        #expect(core.handle(line: #"{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":3}}"#) == .none)
        // A response from the client (we never send requests) is ignored too.
        #expect(core.handle(line: #"{"jsonrpc":"2.0","id":9,"result":{}}"#) == .none)
    }

    @Test func pingAnswersEmpty() throws {
        var core = makeCore()
        let response = try reply(core.handle(line: #"{"jsonrpc":"2.0","id":7,"method":"ping"}"#))
        #expect(response == ["jsonrpc": "2.0", "id": 7, "result": [:]])
    }

    @Test func errorsUseJSONRPCCodes() throws {
        var core = makeCore()
        #expect(try reply(core.handle(line: "{not json"))["error"]?["code"] == -32700)
        #expect(try reply(core.handle(line: "[1,2]"))["error"]?["code"] == -32600)
        #expect(try reply(core.handle(line: #"{"jsonrpc":"2.0","id":1,"method":"resources/list"}"#))["error"]?["code"] == -32601)
        #expect(try reply(core.handle(line: #"{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"format_c"}}"#))["error"]?["code"] == -32602)
        #expect(try reply(core.handle(line: #"{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"click","arguments":[1]}}"#))["error"]?["code"] == -32602)
    }

    @Test func toolsCallBecomesAnAction() {
        var core = makeCore()
        let action = core.handle(line: #"{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"click","arguments":{"session":"s1","x":10,"y":20.5}}}"#)
        #expect(action == .callTool(id: 5, name: "click", arguments: ["session": "s1", "x": 10, "y": 20.5]))
        // Missing arguments mean an empty object.
        #expect(core.handle(line: #"{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"list_sessions"}}"#)
            == .callTool(id: 6, name: "list_sessions", arguments: [:]))
    }

    @Test func toolResultsEncodeContentBlocks() throws {
        let line = MCPServerCore.response(to: 5, result: ToolResult(content: [
            .image(base64: "iVBORw0KGgo=", mimeType: "image/png"), .text("1280×800 px"),
        ]))
        let response = try JSONValue.decode(line)
        #expect(response["id"] == 5)
        #expect(response["result"]?["content"] == [
            ["type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"],
            ["type": "text", "text": "1280×800 px"],
        ])
        #expect(response["result"]?["isError"] == nil)
        let error = try JSONValue.decode(MCPServerCore.response(to: "x", result: .error("paused by the user")))
        #expect(error["result"]?["isError"] == true)
        #expect(error["result"]?["content"]?.arrayValue?.first?["text"] == "paused by the user")
    }

    @Test func toolsListHasEveryTool() throws {
        var core = makeCore()
        let response = try reply(core.handle(line: #"{"jsonrpc":"2.0","id":1,"method":"tools/list"}"#))
        let names = response["result"]?["tools"]?.arrayValue?.compactMap { $0["name"]?.stringValue } ?? []
        #expect(names == [
            "list_connections", "list_sessions", "connect", "disconnect", "screenshot", "click", "mouse_move",
            "mouse_down", "mouse_up", "drag", "scroll", "type_text", "key", "clipboard_get", "clipboard_set",
            "wait", "wait_for_screen", "read_screen", "find_text",
        ])
    }

    @Test(arguments: ToolCatalog.all.map(\.name))
    func schemasAreConsistent(name: String) throws {
        let tool = try #require(ToolCatalog.definition(named: name))
        let schema = tool.inputSchema
        #expect(schema["type"] == "object")
        let properties = try #require(schema["properties"]?.objectValue)
        for required in schema["required"]?.arrayValue ?? [] {
            #expect(properties[try #require(required.stringValue)] != nil, "\(name): '\(required)' is not a property")
        }
        for (key, property) in properties {
            #expect(property["type"]?.stringValue != nil, "\(name).\(key) has no type")
        }
        let sessionless = ["list_connections", "list_sessions", "connect"]
        #expect((properties["session"] != nil) == !sessionless.contains(name))
        #expect(!tool.description.isEmpty)
        // Every input action can return a screenshot.
        let inputs = ["click", "mouse_move", "mouse_down", "mouse_up", "drag", "scroll", "type_text", "key"]
        #expect((properties["screenshot"] != nil) == inputs.contains(name))
        #expect(tool.readOnly == !(inputs + ["connect", "disconnect", "clipboard_set"]).contains(name))
    }
}

struct LineFramerTests {
    @Test func splitsOnNewlinesAcrossChunks() throws {
        var framer = LineFramer()
        #expect(try framer.append(Array(#"{"a":1}"#.utf8) + [0x0A] + Array(#"{"b""#.utf8)) == [#"{"a":1}"#])
        #expect(framer.pendingByteCount == 4)
        #expect(try framer.append(Array(#":2}"#.utf8) + [0x0D, 0x0A, 0x0A]) == [#"{"b":2}"#])
        #expect(framer.pendingByteCount == 0)
    }

    @Test func keepsMultibyteCharactersSplitAcrossChunks() throws {
        var framer = LineFramer()
        let bytes = Array("\"Grüße €\"\n".utf8)
        var lines: [String] = []
        for byte in bytes { lines += try framer.append([byte]) }
        #expect(lines == ["\"Grüße €\""])
    }

    @Test func refusesEndlessLines() {
        var framer = LineFramer(maxLineLength: 8)
        #expect(throws: LineFramer.Failure.lineTooLong(9)) { try framer.append(Array("123456789".utf8)) }
        #expect(framer.pendingByteCount == 0)
    }
}

struct JSONValueTests {
    @Test func integersStayIntegers() throws {
        let value = try JSONValue.decode(#"{"id":1,"x":2.5,"big":12345678901,"flag":true,"none":null}"#)
        #expect(value["id"] == .int(1))
        #expect(value["x"] == .double(2.5))
        #expect(value["big"] == .int(12_345_678_901))
        #expect(value["flag"] == .bool(true))
        #expect(value["none"] == .null)
        #expect(JSONValue.object(["id": 1, "x": 2.5]).encoded() == #"{"id":1,"x":2.5}"#)
    }

    @Test func encodingEscapesNewlinesAndKeepsUnicode() {
        let line = JSONValue.string("a\nb Grüße / \\").encoded()
        #expect(line == #""a\nb Grüße / \\""#)
    }
}
