import json
import unittest

from weitblick.agent import catalog
from weitblick.agent.line_framer import LineFramer, LineTooLong
from weitblick.agent.mcp_core import (CallTool, ImageContent, MCPClientInfo, MCPServerCore, Reply, TextContent,
                                      ToolResult, UNKNOWN_CLIENT, encode_json)


def make_core():
    return MCPServerCore("weitblick-remote", "1.1.0", catalog.INSTRUCTIONS, catalog.TOOLS)


def reply(action):
    assert isinstance(action, Reply), f"expected a reply, got {action}"
    assert "\n" not in action.line
    return json.loads(action.line)


class MCPServerCoreTests(unittest.TestCase):
    def test_initialize_echoes_a_supported_version(self):
        for version in ["2025-06-18", "2025-03-26", "2024-11-05"]:
            with self.subTest(version=version):
                core = make_core()
                response = reply(core.handle_line(json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": version, "capabilities": {},
                               "clientInfo": {"name": "claude-code", "version": "2.1.306"}}})))
                self.assertEqual(response["id"], 1)
                self.assertEqual(response["result"]["protocolVersion"], version)
                self.assertEqual(response["result"]["serverInfo"]["name"], "weitblick-remote")
                self.assertIn("tools", response["result"]["capabilities"])
                self.assertIn("find_text", response["result"]["instructions"])
                self.assertEqual(core.client, MCPClientInfo("claude-code", "2.1.306"))
                self.assertEqual(core.client.display_name, "claude-code 2.1.306")

    def test_unknown_version_gets_the_newest(self):
        core = make_core()
        response = reply(core.handle_line(
            '{"jsonrpc":"2.0","id":"a","method":"initialize","params":{"protocolVersion":"2099-01-01"}}'))
        self.assertEqual(response["id"], "a")
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(core.client, UNKNOWN_CLIENT)

    def test_notifications_get_no_answer(self):
        core = make_core()
        self.assertIsNone(core.handle_line('{"jsonrpc":"2.0","method":"notifications/initialized"}'))
        self.assertTrue(core.is_initialized)
        self.assertIsNone(core.handle_line(
            '{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":3}}'))
        # A response from the client (we never send requests) is ignored too.
        self.assertIsNone(core.handle_line('{"jsonrpc":"2.0","id":9,"result":{}}'))

    def test_ping_answers_empty(self):
        core = make_core()
        self.assertEqual(reply(core.handle_line('{"jsonrpc":"2.0","id":7,"method":"ping"}')),
                         {"jsonrpc": "2.0", "id": 7, "result": {}})

    def test_errors_use_json_rpc_codes(self):
        core = make_core()
        code = lambda line: reply(core.handle_line(line))["error"]["code"]  # noqa: E731
        self.assertEqual(code("{not json"), -32700)
        self.assertEqual(code("[1,2]"), -32600)
        self.assertEqual(code('{"jsonrpc":"2.0","id":1,"method":"resources/list"}'), -32601)
        self.assertEqual(code('{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"format_c"}}'), -32602)
        self.assertEqual(code('{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"click","arguments":[1]}}'),
                         -32602)
        self.assertEqual(code('{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{}}'), -32602)

    def test_tools_call_becomes_an_action(self):
        core = make_core()
        action = core.handle_line('{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"click",'
                                  '"arguments":{"session":"s1","x":10,"y":20.5}}}')
        self.assertEqual(action, CallTool(5, "click", {"session": "s1", "x": 10, "y": 20.5}))
        self.assertIsInstance(action.arguments["x"], int)
        # Missing arguments mean an empty object.
        self.assertEqual(core.handle_line('{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"list_sessions"}}'),
                         CallTool(6, "list_sessions", {}))

    def test_tool_results_encode_content_blocks(self):
        line = MCPServerCore.response(5, ToolResult([ImageContent("iVBORw0KGgo=", "image/png"),
                                                     TextContent("1280×800 px")]))
        response = json.loads(line)
        self.assertEqual(response["id"], 5)
        self.assertEqual(response["result"]["content"], [
            {"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"},
            {"type": "text", "text": "1280×800 px"},
        ])
        self.assertNotIn("isError", response["result"])
        self.assertIn("1280×800", line)  # Unicode stays readable
        error = json.loads(MCPServerCore.response("x", ToolResult.error("paused by the user")))
        self.assertIs(error["result"]["isError"], True)
        self.assertEqual(error["result"]["content"][0]["text"], "paused by the user")

    def test_tools_list_has_every_tool(self):
        core = make_core()
        response = reply(core.handle_line('{"jsonrpc":"2.0","id":1,"method":"tools/list"}'))
        self.assertEqual([tool["name"] for tool in response["result"]["tools"]], [
            "list_connections", "list_sessions", "connect", "disconnect", "screenshot", "click", "mouse_move",
            "mouse_down", "mouse_up", "drag", "scroll", "type_text", "key", "clipboard_get", "clipboard_set",
            "wait", "wait_for_screen", "read_screen", "find_text",
        ])
        self.assertIs(response["result"]["tools"][0]["annotations"]["readOnlyHint"], True)

    def test_schemas_are_consistent(self):
        inputs = ["click", "mouse_move", "mouse_down", "mouse_up", "drag", "scroll", "type_text", "key"]
        for tool in catalog.TOOLS:
            with self.subTest(tool=tool.name):
                self.assertIs(catalog.definition(tool.name), tool)
                schema = tool.input_schema
                self.assertEqual(schema["type"], "object")
                properties = schema["properties"]
                for required in schema.get("required", []):
                    self.assertIn(required, properties)
                for key, prop in properties.items():
                    self.assertIsInstance(prop.get("type"), str, f"{tool.name}.{key} has no type")
                sessionless = ["list_connections", "list_sessions", "connect"]
                self.assertEqual("session" in properties, tool.name not in sessionless)
                self.assertTrue(tool.description)
                self.assertEqual("screenshot" in properties, tool.name in inputs)
                self.assertEqual(tool.read_only,
                                 tool.name not in inputs + ["connect", "disconnect", "clipboard_set"])


class LineFramerTests(unittest.TestCase):
    def test_splits_on_newlines_across_chunks(self):
        framer = LineFramer()
        self.assertEqual(framer.append(b'{"a":1}\n{"b"'), ['{"a":1}'])
        self.assertEqual(framer.pending_byte_count, 4)
        self.assertEqual(framer.append(b':2}\r\n\n'), ['{"b":2}'])
        self.assertEqual(framer.pending_byte_count, 0)

    def test_keeps_multibyte_characters_split_across_chunks(self):
        framer = LineFramer()
        lines = []
        for byte in '"Grüße €"\n'.encode():
            lines += framer.append(bytes([byte]))
        self.assertEqual(lines, ['"Grüße €"'])

    def test_refuses_endless_lines(self):
        framer = LineFramer(max_line_length=8)
        with self.assertRaises(LineTooLong) as raised:
            framer.append(b"123456789")
        self.assertEqual(raised.exception.size, 9)
        self.assertEqual(framer.pending_byte_count, 0)


class JSONEncodingTests(unittest.TestCase):
    def test_integers_stay_integers(self):
        value = json.loads('{"id":1,"x":2.5,"big":12345678901,"flag":true,"none":null}')
        self.assertEqual(encode_json(value), '{"big":12345678901,"flag":true,"id":1,"none":null,"x":2.5}')

    def test_escapes_newlines_and_keeps_unicode(self):
        self.assertEqual(encode_json("a\nb Grüße / \\"), '"a\\nb Grüße / \\\\"')

    def test_non_finite_numbers_become_zero(self):
        self.assertEqual(encode_json({"x": float("nan"), "y": [float("inf")]}), '{"x":0,"y":[0]}')


if __name__ == "__main__":
    unittest.main()
