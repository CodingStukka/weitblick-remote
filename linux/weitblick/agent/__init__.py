"""Agent control over MCP: the pure logic of letting AI agents (Claude Code, Codex, any MCP client)
drive RDP sessions. Port of the macOS app's AgentControl package plus the socket server, audit log
and text recognition. The app glue (sessions, prompts, tools) lives elsewhere and uses these modules:

- mcp_core: MCP over newline-delimited JSON-RPC 2.0 (tools only), without I/O
- catalog: the tools, their schemas and the server instructions
- arguments: typed tool arguments with model-readable errors
- screen_mapping, frame_diff: screenshot <-> desktop pixels, "has the screen settled"
- keycombo, text_typing: the `key` and `type_text` notations as RDP key events
- text_matching, ocr: on-screen text (tesseract) and OCR-tolerant search
- access_gate: per-connection permission (ask / always / never, prompts and grants)
- audit: the agent audit log
- line_framer, socket_server: the Unix socket weitblick-mcp relays to
"""
