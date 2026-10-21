M7 DONE

# M7 Agent control over MCP – Worklog

Agent: M7. Owns `Packages/AgentControl`, `Sources/WeitblickRemote/Agent/**`, `Sources/WeitblickMCP/**`,
`scripts/mcp-e2e.sh` (+ driver), and the small hooks this needs in the app, WeitblickKit, the bridge, KeyboardEngine
and ConnectionStore. Spec: `docs/SPEC.md` → "Agent control over MCP (M7)" (incl. the permission prompt, 1dfb5f7).
Rules: test VM 10.211.55.9 only (VM lock), never the general pasteboard, no focus/activation/OS input, test app
instances run directly (never via LaunchServices) with temp store/socket/log, builds only via `scripts/build.sh`.

## Status

- [x] 1 Plan + worklog
- [x] 2 `Packages/AgentControl` (JSON, JSON-RPC framing, MCP core, tool catalog, arguments, key combos, typing,
  coordinate mapping, access gate, frame diff, text matching, audit formatting) + unit tests (61 green)
- [x] 3 KeyboardEngine: KLID → Mac layout (reverse table) for `key` (113 green); ConnectionStore:
  `security.agentAccess` (ask/always/never, tolerant decoding; 33 green)
- [x] 4 Bridge `wb_session_framebuffer_peek` + WeitblickKit wrapper; ClipboardSync text API for agents
- [x] 5 App glue (`Sources/WeitblickRemote/Agent/`): socket server, host, tools, capture, OCR, prompt panel,
  audit log, overlay/take over, overview badge + Watch, settings, editor
- [x] 6 Helper `weitblick-mcp` (stdio relay, hidden launch) embedded in Contents/MacOS
- [x] 7 Build + unit tests (AgentControl 61, app 66, KeyboardEngine 113, ConnectionStore 33: all green)
- [x] 8 `scripts/mcp-e2e.sh` 3× green against the VM
- [x] 9 Real client check (`claude -p … --model haiku`)
- [x] 10 codesign verify, README section, version 1.1.0 (2), String Catalogs (de), cleanup

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
- **OCR-aware matching (found in the e2e):** Vision reads Notepad's monospace font with typical confusions
  ("ß" → "B", "|" → "l"/"I", "}" → ")", dropped spaces), so a typed string was not found again. Matching now ignores
  whitespace on both sides and folds look-alike classes before the approximate search (`| 1 i ! ı` → l, `0` → o,
  folded "ss" → b, `{[` → (, `}]` → ), quote and dash variants). Ranges still map back to the OCR string. Queries
  under 5 characters stay exact (after folding), so "OK" can't match random two-letter words.
- **Typing pace:** WinUI Notepad dropped and repeated Unicode characters at 8 ms per character ("ÄÖÜ" arrived as
  "ÜÜÜ"). Measured safe pace: 25 ms per character, 20 ms per key action, 50 ms between chords. `type_text` is capped
  at 5,000 characters (~2 min); the tool description points long text to `clipboard_set` + `ctrl+v`.
- **Connect waits for a usable desktop:** right after logon the framebuffer is white for a few seconds. `connect`
  waits (up to 30 s) until a 64×40 sample grid shows at least 6 colours, then for the screen to settle, so the
  agent's first screenshot is the real desktop.
- **Agent-facing text is plain English** (not `String(localized:)`): models read it, and the UI language must not
  change what an agent sees. User-facing UI (prompt, banner, settings, editor, overview) is localized (32 new keys,
  German added to `Localizable.xcstrings`, key order kept case-insensitive like Xcode writes it).
- **Clean quit removes the socket:** `applicationWillTerminate` stops the server (socket file unlinked); SIGTERM
  goes through the normal terminate path without the "open sessions" question. A stale socket left by a crash is
  detected by a connect probe and replaced.
- **Helper:** checks the master switch itself (CFPreferences in the app's domain) so a turned-off app is never
  launched just to say no; launches the app hidden (`--agent-launch`, accessory policy, no activation) and waits
  up to 30 s for the socket. `WEITBLICK_MCP_APP` lets tests start a wrapper script instead of LaunchServices.
- **clipboard_get waits briefly for the remote change:** after ctrl+c the remote clipboard arrives a moment later;
  `clipboard_get` waits up to 1.5 s after the last input for the remote change count to move.
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
- `scripts/mcp-e2e.sh` + `scripts/mcp-e2e.py` written (VM lock, temp app copy with bundle id
  `nrw.neuhaus.weitblick-remote.m7test`, ad-hoc re-signed, own defaults domain, temp store/socket/log, binary run
  directly via a wrapper, never LaunchServices). First runs found: garbled Unicode typing (→ pacing), white first
  screenshot (→ desktop wait), OCR misses (→ OCR-aware matching), socket left after SIGTERM (→ shutdown on
  terminate), non-blocking accepted sockets (→ blocking). All fixed (d8bd25b).
- E2E 3/3 green, master switch off green (helper alone and app), agent-launch auto-quit green:
  `build/evidence/m7-mcp-e2e-20261021-155244`. `claude -p --model haiku` check green (claude-code 2.1.306 called
  list_connections → connect → screenshot → disconnect and described the desktop):
  `build/evidence/m7-mcp-e2e-20261021-155742`.
- German translations for the 32 new UI strings; README section "Use with AI agents (MCP)".
- Final round on the final build (`scripts/mcp-e2e.sh --takeover --no-build --runs 3 --claude`): runs 1–3 PASS
  (91 checks), master switch off PASS, Claude Code 2.1.306 (haiku) connect → screenshot → disconnect and described
  the desktop: `build/evidence/m7-mcp-e2e-20261021-160417`. Unit tests: AgentControl 61, app 66, KeyboardEngine
  113, ConnectionStore 33, all green. `codesign --verify --strict --deep` valid (helper included), version 1.1.0 (2).
- Note: the VM's desktop shows the Mac's Desktop folder through Parallels' shared profile, so screenshots in
  `build/evidence` contain file names from Marc's Mac desktop. They stay in the gitignored build/ folder.

## Not verified live (Marc should try)

- Prompt panel, take-over banner, Watch button, overview badge and Settings section were not looked at on screen
  (Marc works on this Mac; tests answer prompts through the DEBUG hook). Worth one manual pass: Ask prompt with
  sound, Allow for 10 minutes → listed in Settings → Revoke, Watch → Take over → Resume agent.
- The production launch path (helper → NSWorkspace launch of `/Applications/Weitblick Remote.app` hidden) was not
  run, by rule; tests use `WEITBLICK_MCP_APP`. First real launch may also show the macOS Local Network prompt.
- Attaching to a session the user opened, `connect(show: true)` and the 30-minute idle close are implemented but
  not covered by the e2e.

## Known gaps

- `DisconnectReason` texts (host unreachable, logon failed, …) reach agents in the UI language (German on Marc's
  Mac); everything the agent layer writes itself is English.
- `type_text` is limited to 5,000 characters at ~40 characters per second (WinUI Notepad needs the pace).
- The helper's code signing identifier is `weitblick-mcp` (ad-hoc), so macOS shows that name if it ever asks about
  it.

## Risks / open

- OCR accuracy: solved for the tested cases by 2× upscaling before Vision plus OCR-aware matching; very small or
  low-contrast text may still be missed (find_text then returns the visible lines so the agent can adapt).
- PNG vs JPEG: PNG up to 1 MB, JPEG (quality 0.85) above. Measured: the Windows 11 wallpaper desktop goes out as
  JPEG, screens with an app window (Notepad) as PNG.
