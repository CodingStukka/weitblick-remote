# Weitblick Remote: RDP client for macOS

Goal: replace Jump Desktop as Marc's RDP client. A native Mac app (Apple Silicon, macOS 14+),
open source (Apache-2.0), that connects to Windows computers over RDP cleanly, reliably and fast.
The core features are the **keyboard** (including translating Mac shortcuts as Jump does) and the **clipboard**.

## Real target systems (from Marc's Jump configuration)

- 11 RDP targets (plus 1 VNC): customer PCs by NetBIOS host name, port 3389,
  NLA on, some with a domain, no gateway. Older industrial PCs (possibly Windows 7/10 IoT) are possible:
  legacy TLS and RDP security as a fallback must work.
- Drive redirection of `~/Downloads` and a project folder on almost every connection.
- Clipboard on, audio playback on, printer redirection on, dynamic resolution on.
- Jump files: `~/Library/Containers/com.p5sys.jump.mac.viewer/Data/Documents/JumpDesktop/Viewer/Servers/*.jump`
  (JSON). The import takes over these settings (not the passwords; they are not in the files).
- VNC target m4-mini: not our concern, macOS Screen Sharing covers it.

**Development rule:** connect to the test VM only. The customer PCs are production systems;
Marc tests those himself.

## Test environment

- Parallels VM "Windows 11" (Pro 24H2, ARM, German, keyboard de-DE), IP `10.211.55.9`, RDP enabled.
- Local test user (Administrator + Remote Desktop Users). Credentials in `.testvm.env`
  (gitignored): `WEITBLICK_TEST_HOST`, `WEITBLICK_TEST_USER`, `WEITBLICK_TEST_PASS`.
- Commands in the VM without RDP: `prlctl exec "Windows 11" powershell -NoProfile -Command "…"`
  (runs as SYSTEM, not in the RDP session).

## Architecture

- **RDP core: FreeRDP 3** (Apache-2.0), as the Git submodule `vendor/FreeRDP` at tag `3.32.1`, built
  statically (arm64, deployment target 14.0) by `scripts/build-freerdp.sh`. OpenSSL 3 static.
  Channels built in statically: drdynvc, rdpgfx, disp, cliprdr, rdpsnd (mac), rdpdr + drive (+ printer later),
  audin (later). No X11/SDL/Wayland/FFmpeg. H.264 later through an own VideoToolbox backend.
  Why FreeRDP rather than RDPKit/IronRDP: mature with NLA/NTLM/domains, legacy TLS for old Windows,
  drive and printer redirection, gateway. Marc's targets need these.
- `Sources/WeitblickBridge` (C): thin bridge to libfreerdp (session life cycle, event loop thread,
  callbacks for frames/pointer/errors/certificate/sign-in, input functions).
- `Packages/KeyboardEngine` (pure Swift package, without FreeRDP): translates Mac key events into
  RDP input actions. Fully unit-tested.
- `Sources/WeitblickRemote` (app, AppKit + SwiftUI): connection list, settings, session window.
  UI language English, German translation in String Catalogs (`Localizable.xcstrings`, `InfoPlist.xcstrings`,
  plus the ConnectionStore package's own catalog for import notes).
- Project via XcodeGen (`project.yml` committed, `.xcodeproj` not). Not sandboxed (drives,
  event tap), hardened runtime. Signed ad-hoc by default; an optional, gitignored `Local.xcconfig`
  sets a personal identity (stable TCC grants for Accessibility, see docs/RELEASING.md).

## Keyboard (core feature)

The engine's output is actions: `scancode(code, extended, down)`, `unicode(codepoint, down)`,
`sync(capsLock, numLock, scrollLock)`.

### Physical mapping
- macOS key code → RDP scancode (set 1) + extended flag. Right modifiers, arrows, Home/End/Page Up/Page Down,
  Forward Delete, keypad Enter and keypad / are extended.
- ISO keyboards (German MacBook): `kVK_ISO_Section` (0x0A, key left of 1, ^°) → 0x29,
  `kVK_ANSI_Grave` (0x32, key left of Y, <>) → 0x56. On ANSI: 0x32 → 0x29. Map the JIS keys too.
- Help → Insert, F13 → Print Screen, F14 → Scroll Lock, F15 → Pause, keypad Clear → Num Lock.
- Keyboard layout of the session = current Mac layout (TIS input source → Windows KLID, e.g. German
  → 0x0407, Swiss German → 0x0807, US → 0x0409, British → 0x0809, …); can be overridden per connection.

### "Mac shortcuts" mode (default) vs. "Windows 1:1"
In 1:1: ⌘ = Win, ⌥ = Alt, ⌃ = Ctrl, everything as scancodes.

In "Mac shortcuts":
- **⌘ is held back** (modifier deferral): the next key decides. Tapping ⌘
  alone = tapping the Windows key (Start menu).
- Default rule: ⌘ + key → Ctrl + the same physical key (⌘C/V/X/A/Z/S/F/L/T/W/R/N/P …).
- Special rules:
  - ⌘⇧Z → Ctrl+Y
  - ⌘← / ⌘→ → Home / End (selecting with ⇧); ⌘↑ / ⌘↓ → Ctrl+Home / Ctrl+End (with ⇧)
  - ⌥← / ⌥→ / ⌥↑ / ⌥↓ → Ctrl+arrow (with ⇧); ⌥⌫ → Ctrl+Backspace; ⌥⌦ → Ctrl+Delete
  - ⌘⌫ → ⇧Home, Backspace (delete to the start of the line); ⌘⌦ → ⇧End, Delete
  - ⌘⇥ (held, ⇥ repeatedly) → Alt held + ⇥, ⌘⇧⇥ likewise; Alt is released only when ⌘ is released
  - ⌘Space → Win (Start search); ⌥⌘Esc → Ctrl+⇧+Esc (Task Manager)
  - ⌃⌥⌫ (or ⌃⌥⌦) → Ctrl+Alt+Delete
  - ⌘Q → Alt+F4 (as in Jump; quit Weitblick Remote through the menu or ⌘Q outside a session),
    ⌘[ / ⌘] → Alt+← / Alt+→
- Stay local: ⌃⌘F (full screen), everything with ⌃⌥⌘ (reserved for app shortcuts).
- ⌃ stays Ctrl (so ⌃C works too).
- macOS takes system shortcuts (⌘⇥, ⌘Space, ⌃←/→ etc.) before the app sees them. Optionally captured
  with a CGEventTap (needs the Accessibility permission): default "in full screen", selectable "always"/"never".

### Option key (⌥)
Problem: on a German Mac, @ comes from ⌥L, € from ⌥E, { } [ ] | \ ~ from ⌥5/6/8/9/7, ⌥⇧7, ⌥N.
Windows has them on AltGr + other keys. Strategies (per connection, default "Smart"):
- **Smart:** if ⌥ + key produces a "useful" character in the Mac layout (printable ASCII,
  €, „ “ ‚ ‘ « » – — … ° § and similar) → send it as Unicode. Otherwise (ƒ, ∂, …) → Alt + key
  (accelerator, e.g. ⌥F → Alt+F, ⌥D → Alt+D). ⌥ + function keys/arrows/Tab → Alt (⌥F4 = Alt+F4).
- **Jump style:** right ⌥ = special characters (Unicode), left ⌥ = Alt.
- **Always Alt** / **Always characters**.
- Dead keys (^ ´ ` ¨ ~) are composed locally in the Unicode path (UCKeyTranslate with deadKeyState).

### Text without modifiers
Scancodes by default (games and keyboard shortcuts work, the layout is synchronized).
Per-connection option "Unicode input": every printable character as Unicode (for a layout mismatch).

### State
- Key repeat: pass repeated key-downs through.
- Focus loss, window inactive, session switch: release every pressed key (no stuck
  modifiers). Focus gain: send `sync` with Caps/Num/Scroll. Num Lock always on (the Mac has none).
- Caps Lock: on a state change, press and release the Caps scancode.
- The rules are a Codable configuration (JSON); the UI starts with switches, an editor comes later.

## Clipboard
Automatic sync in both directions: text (Unicode, line endings), HTML, RTF, images
(PNG/TIFF ↔ DIB), files (FileGroupDescriptorW + FileContents, lazy, in both directions).
Maximum size configurable (default 128 MB).

## Windows and tabs (like Jump, part of M4)
- One main window with a tab bar on top: the first tab "Overview" (connection list), then one tab per connected
  session, named after the connection. Built with native macOS window tabs (shared
  `tabbingIdentifier`, a new session via `addTabbedWindow`), so dragging a tab out = its own window,
  merging windows and full screen with the tab bar on hover work without own code.
- The overview tab always stays the first tab and cannot be closed while sessions are open
  (⌘W there does not quit the app). Double-clicking a connection: if a session already runs, its
  tab is activated instead of opening a second one.
- Closing a session tab = disconnecting (with a confirmation that can be turned off). Status shown in the tab
  (connecting / disconnected / reconnecting).
- Switching tabs: click, ⌃⌥⌘← / ⌃⌥⌘→, ⌃⌥⌘1 = overview, ⌃⌥⌘2…9 = sessions (⌃⌥⌘ is reserved for local use
  anyway; ⌃⇥ and ⌘⇧[ ] go to Windows in a session). A tab switch = focus loss for the keyboard
  (release every key).
- Option "Open sessions in separate windows" for multi-monitor work.

## Migration from Jump (required, part of M4)
Every connection from Jump is taken over, automatically on the first start (with a preview and
confirmation) and again at any time through "File → Import from Jump Desktop…". Idempotent via Jump's `UniqueId`.
- Source: `…/com.p5sys.jump.mac.viewer/Data/Documents/JumpDesktop/Viewer/Servers/*.jump` (JSON).
- Fields: `DisplayName`, `TcpHostName`, `TcpPort`, `Username`, `Domain`, `ProtocolTypeCode`
  (0 = RDP, 1 = VNC), `DriveMappings[]` (name, path, enabled) + `RdpDriveRedirection`,
  `ClipboardRedirection`, `AudioPlaybackCode`, `AudioInputDevice`, `RdpPrinterRedirection`,
  `DefaultPrinter`, `UseDynamicResolutionUpdate`, `MatchScreenResolution`, `ResolutionWidth/Height`,
  `UseHIDPIResolution`, `DesktopScaleFactor`, `StartInFullscreen`, `UseAllMonitors`/`MonitorCount`,
  `KeyboardLocaleId` + `KeyboardAutomaticLocaleDetection`, `RdpUseUnicodeKeyboard`, `RdpDisableNLA`,
  `RdpConsoleSession`, `IgnoreCertificateErrors`, `SslCertificateFingerPrint` (taken over as
  trusted), `RdpAlternateShellPath/WorkingDir`, `LoadBalancerInfo`, `RDGatewayUniqueId`, `MacAddresses`
  (Wake-on-LAN), `Tags`, `LastConnectedTime`. Unknown fields are listed in the import report instead of dropped.
- VNC entries (m4-mini) are taken over too and open macOS Screen Sharing (`vnc://host:port`).
- Passwords are neither in the files nor readable in the keychain (Jump is sandboxed). On the
  first connection Weitblick Remote asks once and saves the password in the keychain.
- Keyboard profile: `…/Library/Application Support/Jump Desktop/JDInputProfile.plist` (NSKeyedArchiver,
  profiles "Mac" and "Windows"). Marc's profiles are Jump's defaults. Taken over from them into Weitblick Remote's
  default rules: ⌘Q → Alt+F4 (in the session, not quitting the app), ⌘[ / ⌘] → Alt+← / Alt+→, ⌘⇧Z → Ctrl+Y,
  ⌃⌥⌫ → Ctrl+Alt+Delete. Custom mappings that differ in the profile are imported as rules.
- Afterwards an import report: what was taken over and what was not (with the reason).

## Agent control over MCP (M7)
AI agents (Claude Code, Codex, any MCP client) use Weitblick Remote as a "computer use" tool for Windows
machines: screenshots, mouse, keyboard, clipboard and on-screen text over RDP. The point over desktop
automation on the Mac: everything travels inside the RDP protocol, so an agent never moves the Mac's
mouse, never types into the Mac, never takes focus and never touches the Mac's pasteboard. Marc keeps
working while an agent drives a Windows GUI (for example AutoCAD on a lab PC).

### Shape
- **The app is the host.** It owns sessions, the connection store, the keychain passwords and trusted
  certificates. It serves MCP (JSON-RPC 2.0, newline-delimited, tools only) on a Unix socket
  `~/Library/Application Support/Weitblick Remote/mcp.sock` (mode 0600; path overridable for tests).
- **`weitblick-mcp`**, a small stdio helper in `Weitblick Remote.app/Contents/MacOS/`, relays stdin/stdout
  to the socket. If the app does not run, it launches it hidden in the background (no activation, no
  window) and waits for the socket. Setup is one line:
  `claude mcp add weitblick -- "/Applications/Weitblick Remote.app/Contents/MacOS/weitblick-mcp"`.
- Pure logic (MCP framing, tool schemas, key-combo parsing, coordinate mapping) lives in a local package
  `Packages/AgentControl` with unit tests; app glue in `Sources/WeitblickRemote/Agent/`.

### Safety gates
- Settings → "AI agents (MCP)": master switch, default off. Off = the socket is not even created.
- Per connection (editor, Security page) "AI agent access": **Ask** (default) · **Always allow** ·
  **Never**. "Never" refuses with a clear message that says where to change it (for production PCs).
- **Ask** shows a permission prompt when an agent wants the connection (connect, or attach to an open
  session). It names the MCP client (from `initialize` clientInfo) and the connection (name, host) and
  offers: **Deny** · **Allow once** · **Allow for 10 minutes** · **Allow today** · **Always allow**.
  - The prompt is a floating, non-activating panel that also shows while the app runs hidden; it does
    not take keyboard focus (plus a notification sound). The tool call waits up to 2 minutes for an
    answer, then fails with "no answer from the user".
  - Allow once = this session until it disconnects. 10 minutes / today (until local midnight) = every
    connect and tool call on that connection in that window; after expiry the next tool call asks again.
    Always allow = switches the connection to "Always allow" (persisted). Deny = this request only.
  - Time grants live in memory only (an app restart asks again). Settings lists active grants with
    "Revoke"; after a revoke the next tool call on that connection asks again.
  - One prompt per connection at a time; parallel requests for the same connection wait for that answer.
  - Tests answer the prompt through a DEBUG launch hook, never by clicking.
- Unknown or changed certificates and missing passwords never get resolved by an agent: the tool
  fails and asks the user to connect once by hand.
- Audit log of every tool call (time, client, connection, tool, arguments; typed text logged by length
  only) in `~/Library/Logs/Weitblick Remote/agent.log`.

### Sessions
- Agent sessions run inside the app **without a window** by default. The overview lists them with an
  agent badge and a "Watch" button; only a user action opens the tab. `connect` with `show: true`
  opens a tab without activating the app.
- A visible tab of an agent-controlled session blocks local input with an overlay ("An agent is
  controlling this session · Take over"). Take over pauses the agent (its tools return "paused by the
  user") until "Resume agent".
- An agent may attach to a session the user already has open on an allowed connection; the overlay
  then appears in that tab.
- Agent sessions use a fixed desktop size, default 1280×800 at 100 % scale, so screenshot pixels are
  click coordinates. Attached user sessions are downscaled for screenshots (long side ≤ 1280) and
  coordinates are mapped back; tool results state the scale.
- Agent-only sessions never sync with the Mac's general pasteboard (isolated clipboard).
- Sessions an MCP client opened close when that client disconnects, and after 30 min without a tool call.
- Windows client editions allow one session: connecting kicks the console user, like a normal login.

### Tools
Coordinates are in screenshot pixels. Input actions accept `screenshot: true` to return a screenshot
once the screen has settled.
- `list_connections` (name, host, agent allowed, open sessions), `list_sessions`
- `connect(connection, width?, height?, show?)` → session id and desktop size, once the desktop is up;
  `disconnect(session)`
- `screenshot(session, region?)` → image (PNG, JPEG when large) plus size, scale, cursor position
- `click(session, x, y, button?, count?, modifiers?)`, `mouse_move`, `mouse_down`, `mouse_up`,
  `drag(session, path, button?)`, `scroll(session, x, y, dx, dy)` (wheel notches)
- `type_text(session, text)`: Unicode input events, independent of the keyboard layout; `\n` = Enter,
  `\t` = Tab
- `key(session, keys, repeat?)`: Windows combos like `ctrl+shift+esc`, `win+r`, `alt+f4`, `enter`,
  `f5`. Letters and symbols are resolved through the session's keyboard layout, so `ctrl+z` presses the
  key that types "z" (on German layouts the physical Y key)
- `clipboard_get(session)` / `clipboard_set(session, text)`: text over cliprdr
- `wait(session, seconds)`, `wait_for_screen(session, timeout?, settle_ms?)`: until frames stop changing
- `read_screen(session, region?)` / `find_text(session, query, region?)`: on-device OCR (Apple Vision,
  German and English) with boxes and click points in screenshot coordinates

## Milestones
- **M1** Core: FreeRDP build, bridge, session window with picture, mouse, scrolling, cursor shapes,
  dynamic resolution, Retina. Headless smoke test.
- **M2** KeyboardEngine package with tests (in parallel with M1).
- **M3** Integration of keyboard + clipboard (text, images), E2E tests against the VM.
- **M4** Connection management, Jump import, keychain, certificate trust (TOFU), sign-in dialog.
- **M5** Drives, audio, files over the clipboard, auto-reconnect, full screen polish.
- **M6** H.264 via VideoToolbox, printers, multi-monitor, gateway (as needed).
- **M7** Agent control over MCP (see above), released as 1.1.0 after Marc's go.

Done means: Marc uses Weitblick Remote instead of Jump for his RDP targets.
