M7 IN PROGRESS

# M7 Agent control over MCP – Worklog

Agent: M7. Owns `Packages/AgentControl`, `Sources/WeitblickRemote/Agent/**`, `Sources/WeitblickMCP/**`,
`scripts/mcp-e2e.sh` (+ driver), and the small hooks this needs in the app, WeitblickKit, the bridge, KeyboardEngine
and ConnectionStore. Spec: `docs/SPEC.md` → "Agent control over MCP (M7)" (incl. the permission prompt, 1dfb5f7).
Rules: test VM 10.211.55.9 only (VM lock), never the general pasteboard, no focus/activation/OS input, test app
instances run directly (never via LaunchServices) with temp store/socket/log, builds only via `scripts/build.sh`.

## Status

- [x] 1 Plan + worklog
- [x] 2 `Packages/AgentControl` (JSON, JSON-RPC framing, MCP core, tool catalog, arguments, key combos, typing,
  coordinate mapping, access gate, frame diff, text matching, audit formatting) + unit tests (60 green)
- [x] 3 KeyboardEngine: KLID → Mac layout (reverse table) for `key` (113 green); ConnectionStore:
  `security.agentAccess` (ask/always/never, tolerant decoding; 33 green)
- [x] 4 Bridge `wb_session_framebuffer_peek` + WeitblickKit wrapper; ClipboardSync text API for agents
- [x] 5 App glue (`Sources/WeitblickRemote/Agent/`): socket server, host, tools, capture, OCR, prompt panel,
  audit log, overlay/take over, overview badge + Watch, settings, editor
- [x] 6 Helper `weitblick-mcp` (stdio relay, hidden launch) embedded in Contents/MacOS
- [ ] 7 Build + unit tests (AgentControl, app, KeyboardEngine, ConnectionStore)
- [ ] 8 `scripts/mcp-e2e.sh` 3× green against the VM
- [ ] 9 Real client check (`claude -p … --model haiku`)
- [ ] 10 codesign verify, README section, version 1.1.0 (2), String Catalogs (de), cleanup

## Plan / design

### Shape
- `Packages/AgentControl` (pure Swift, depends on KeyboardEngine for chords): everything testable without the app.
- App glue in `Sources/WeitblickRemote/Agent/`: `AgentHost` (main actor) owns the socket server, the MCP client
  connections, the agent session registry, the access gate, the audit log and implements the tools.
- Agent sessions are `SessionWindowController`s in an "agent" mode whose window is simply never shown until the user
  clicks "Watch" (or `connect(show: true)`): reuses connecting, status, overview activity, overlay and the take-over
  input path instead of a second session stack.
- `weitblick-mcp`: tiny Swift tool in `Contents/MacOS/`, relays stdin/stdout ↔ Unix socket.

## Decisions

- **Accepted sockets are made blocking:** on macOS `accept` inherits O_NONBLOCK from the listener, and a
  screenshot reply (~1–2 MB base64) is larger than the socket buffer; replies are written blocking on a per-client
  queue, and `close()` calls `shutdown` first so a peer that stopped reading can't wedge that queue.
- **Agent launch quits by itself:** `--agent-launch` without a running MCP server (switch off, socket taken) quits
  at once; with a server it quits 60 s after the last client and session are gone, unless the user opened a
  window. The helper never reopens a running app (reopening could show or hide the user's windows).

- **Spec change mid-task (coordinator, Marc, commit 1dfb5f7):** the per-connection gate became a permission prompt
  instead of a plain opt-in toggle: "AI agent access" = Ask (default) / Always allow / Never; Ask shows a floating
  non-activating panel (Deny / Allow once / 10 minutes / today / Always allow), 2 min wait, time grants in memory,
  Settings lists grants with Revoke, one prompt per connection, DEBUG launch hook answers in tests. Implemented as
  `AccessGate` in AgentControl (clock, calendar, durations injected) and `SecuritySettings.agentAccess`.
- **Hand-rolled MCP, no swift-sdk.** Tools-only over newline-delimited JSON-RPC is ~170 lines (`MCPServerCore`:
  initialize with version negotiation 2025-06-18 / 2025-03-26 / 2024-11-05, notifications, ping, tools/list,
  tools/call → action, JSON-RPC errors for protocol faults, `isError` results for tool faults). The swift-sdk would
  add a dependency tree (swift-nio/async plumbing, its own transport abstractions) for features we don't use
  (resources, prompts, sampling) and a moving API; a pure synchronous `handle(line:)` is trivially unit-testable.
- **JSONValue keeps ints apart from doubles** so ids and coordinates round-trip exactly (`1` never becomes `1.0`).
- **Socket transport:** the app listens on a Unix socket; the stdio helper is a dumb byte relay. All protocol
  logic stays in the app (one implementation, one place to test). 32 MB line limit (screenshots are ≤ ~2 MB).
- **Gate semantics (my reading of the spec, unit-tested):** every connect *and* every tool call on a session runs
  through `AccessGate.check` with the connection's current policy (so switching a connection to Never or Ask takes
  effect on the next call). "Allow once" is bound to the session id (for connect the id is allocated before the
  check); a parallel request for a different session that waited on that prompt asks again. Deny refuses the
  request and everything that waited on the same prompt, and remembers nothing. Timeout refuses all waiters.
  "Today" = next local midnight via `Calendar` (DST-safe, tested over the 2026-10-25 switch). Always allow calls
  back into the app, which persists the policy.
- **Key combos reuse KeyboardEngine:** `KeyCombo` only normalizes names models use (super, pgup, arrowleft,
  ctrl++, minus…) and refuses `cmd` with a hint; `WindowsChord` parses and `KeyboardEngine.tap` resolves characters
  through a Mac layout. For agent input the layout is chosen from the session's Windows KLID (new reverse lookup
  `WindowsKeyboardLayoutID.macInputSourceID` + `UCKeyTranslateLayoutProvider(windowsLayout:)`, keyboard type ANSI
  for US, JIS for Japanese, ISO otherwise), not from the Mac's current input source: the remote layout is what
  decides which key types "z". Tested: ctrl+z on German = physical Y (0x15), on US = 0x2C; ctrl++ on German = 0x1B.
- **Text typing:** Unicode events per UTF-16 unit (surrogate pairs as two units, like the KeyboardEngine), `\n`,
  `\r`, `\r\n` = one Enter scancode, `\t` = Tab, other control characters dropped. Grouped per character so the
  app can pace long texts.
- **Coordinates:** one rule for all sessions: long side > 1280 → scale = 1280 / long side, else 1. Agent sessions
  default to 1280×800 (scale 1); `connect` accepts 800–1920 × 600–1200. Points outside the screenshot are refused
  (a model error is better visible than a silent clamp); regions are clipped.
- **Settle detection:** poll the framebuffer and diff (`FrameDiff.changedBounds`, row memcmp then column scan);
  changes ≤ 4 px wide and ≤ 48 px high (a caret) don't count. Avoids depending on frameReady callbacks, which
  windowless sessions would never re-arm (see bridge note below).
- **find_text matching:** per-character folding (case, diacritics, width) so ranges map back to the OCR string
  (needed for Vision's per-range boxes); Sellers approximate search with one error per five characters; exact
  matches suppress approximate ones.
- **Audit log:** one line per tool call (ISO time, client, connection, tool, JSON args with `text` replaced by
  `<N characters>`, ok/error, duration). Results are never logged (clipboard contents stay out).
- **ConnectionStore:** `SecuritySettings` got a hand-written `init(from:)` with `decodeIfPresent` defaults because
  synthesized Codable would throw `keyNotFound` on Marc's existing `connections.json`; unknown agent access values
  fall back to Ask (the safe side). No schema bump needed.

## Log

- AgentControl package + tests written: 60 tests green (`swift test`).
- KeyboardEngine: reverse KLID lookup + `init(windowsLayout:)`, 2 new tests, 113 green.
- ConnectionStore: `agentAccess`, 1 new test (old JSON → ask, unknown → ask, never round-trips), 33 green.
- Bridge `wb_session_framebuffer_peek` (copies without taking dirty rects, so a watched agent session keeps
  drawing); ClipboardSync agent API (`remoteText`, `offerText`, `remoteChangeCount`), Mac pasteboard untouched.
- App glue written: socket server, host, 19 tools, capture/settle, Vision OCR, prompt panels, audit log, banner +
  take over, overview badge + Watch, Settings section, editor picker, SIGTERM quit, DEBUG agent flags.
- Helper `weitblick-mcp` (stdio relay, master-switch check via the app's defaults domain, hidden launch;
  `WEITBLICK_MCP_APP` spawns a test executable instead). Embedded via XcodeGen `copy: executables`.
- First full build green (`scripts/build.sh`); `codesign --verify --strict --deep` valid, helper hardened.

## Not verified live (Marc should try)

## Risks / open
- OCR accuracy for small UI text at 1280×800 (may need 2× upscaling before Vision).
- PNG vs JPEG threshold: to be measured on real screens.
- Unicode typing pace in WinUI Notepad.
