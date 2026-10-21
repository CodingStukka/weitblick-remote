<p align="center">
  <img src="site/assets/img/icon-512.png" width="128" height="128" alt="Weitblick Remote icon">
</p>

<h1 align="center">Weitblick Remote</h1>

<p align="center">
  <strong>Remote Desktop for Windows PCs, made for the Mac. Your Mac shortcuts just work.</strong><br>
  A free, open-source RDP client for macOS, built on FreeRDP.
</p>

<p align="center">
  <a href="https://github.com/mneuhaus/weitblick-remote/releases/latest/download/Weitblick-Remote.dmg"><strong>Download (DMG)</strong></a> ·
  <a href="https://github.com/mneuhaus/weitblick-remote/releases/latest/download/Weitblick-Remote.zip">ZIP</a> ·
  <a href="https://github.com/mneuhaus/weitblick-remote/releases">All releases</a> ·
  <a href="https://mneuhaus.github.io/weitblick-remote/">Website</a>
  <br>
  <sub>macOS 14 Sonoma or later · Apple Silicon · English and German interface · Apache-2.0</sub>
</p>

<p align="center">
  <img src="site/assets/screenshots/session-light.png" width="860" alt="Weitblick Remote with an overview tab and two session tabs, showing a Windows 11 desktop">
</p>

## Features

- **Mac shortcuts translated like Jump Desktop:** ⌘C/V/X/Z become Ctrl shortcuts, ⌘←/→ is Home/End, ⌥←/→
  jumps by word, ⌘⇥ is Alt+Tab, ⌘Q is Alt+F4. Or switch a connection to “Windows 1:1”.
- **German and international layouts:** the session uses your Mac’s layout; ⌥ symbols such as @ € { } [ ] | \ ~
  arrive as typed, dead keys compose as on the Mac.
- **Clipboard in both directions:** text, HTML, RTF, images, and files or whole folders (up to 2 GB per copy).
- **Native tabs:** an overview tab plus one tab per session; drag a tab out for its own window.
- **Folder sharing, audio, printers:** Mac folders as drives, sound on the Mac or the PC, Mac printers in Windows,
  optional microphone.
- **Auto-reconnect** to the same Windows session after a network drop.
- **Jump Desktop import** on first start (with preview), again any time from the File menu; `.rdp` files open too.
- **Keychain:** passwords are stored in the macOS Keychain, on this Mac only.
- **Certificate trust:** see the fingerprint once, trust it permanently, get warned when it changes.
- **Wake-on-LAN** from the connection’s context menu.
- **Retina:** full-resolution sessions that follow the window size.
- **Older Windows:** NLA can be switched off per connection; TLS 1.0 and classic RDP security as fallbacks.

## Install

The app is ad-hoc signed and **not notarized** (no paid Apple Developer account; this is a free project, and the
source and release build script are public). macOS asks you to confirm it once:

1. Open the DMG and drag **Weitblick Remote** into Applications, then open the app.
2. macOS blocks it (“Weitblick Remote” Not Opened). Click **Done**.
3. Open **System Settings → Privacy & Security**, scroll to **Security**, click **Open Anyway** next to the
   note about Weitblick Remote, and confirm with **Open Anyway** and your password or Touch ID.

Terminal alternative:

```sh
xattr -dr com.apple.quarantine "/Applications/Weitblick Remote.app"
```

On macOS 14 you can also Control-click the app and choose **Open**.

Permissions: macOS asks for **Local Network** access the first time you connect to a PC in your network (allow it,
or connections fail with “host unreachable”). Optional: **Accessibility** (to pass ⌘⇥, ⌘Space and ⌃←/→ to Windows)
and **Microphone** (only when enabled for a connection). Because of the ad-hoc signature, macOS asks again for
Accessibility, the microphone and Keychain access after each update.

## Keyboard

Default rules in the “Mac shortcuts” mode (add ⇧ to the navigation shortcuts to select). The full table is on the
[website](https://mneuhaus.github.io/weitblick-remote/#keyboard).

| On the Mac | In Windows |
| --- | --- |
| ⌘ + key (⌘C, ⌘V, ⌘X, ⌘Z, ⌘A, ⌘S …) | Ctrl + the same key |
| ⌘⇧Z | Ctrl+Y |
| ⌘← / ⌘→ · ⌘↑ / ⌘↓ | Home / End · Ctrl+Home / Ctrl+End |
| ⌥← / ⌥→ · ⌥⌫ | Ctrl+← / Ctrl+→ · Ctrl+Backspace |
| ⌘⌫ / ⌘⌦ | delete to line start / end |
| ⌘⇥ (keep ⌘ held) | Alt+Tab |
| ⌘Q | Alt+F4 |
| ⌘Space · ⌘ tapped alone | Windows key |
| ⌥⌘Esc | Ctrl+Shift+Esc |
| ⌃⌥⌫ | Ctrl+Alt+Del |
| ⌘[ / ⌘] | Alt+← / Alt+→ |
| ⌥ + key | the character your Mac layout types (@ € [ ] { } …), otherwise Alt + key |

Stays on the Mac: ⌃⌘F (full screen) and ⌃⌥⌘ shortcuts (tabs). ⌘⇥, ⌘Space and ⌃←/→ reach Windows only with the
Accessibility permission (default: in full screen). In “Windows 1:1” mode, ⌘ is the Windows key, ⌥ is Alt and ⌃ is
Ctrl.

## Use with AI agents (MCP)

Weitblick Remote is also an MCP server: Claude Code, Codex or any other MCP client can open one of your connections,
take screenshots, click, type, read on-screen text and use the clipboard. Everything travels inside the RDP
connection. The agent never moves your Mac’s pointer, never types into your Mac apps and never takes focus.

1. Turn on **Settings → AI agents (MCP)**. It is off by default; while it is off, nothing listens.
2. Add the server to your agent. For Claude Code:

   ```sh
   claude mcp add weitblick -- "/Applications/Weitblick Remote.app/Contents/MacOS/weitblick-mcp"
   ```

   Other clients start the same `weitblick-mcp` command over stdio. It talks to the app over a socket only your user
   account can open (`~/Library/Application Support/Weitblick Remote/mcp.sock`) and starts the app in the background
   if it isn’t running.

**Who may do what.** Each connection has **AI agent access** (Edit… → Security): **Ask** (the default), **Always
allow** or **Never**. Ask shows a small prompt naming the agent and the PC, without taking focus: **Deny**, **Allow
once**, **Allow for 10 minutes**, **Allow today** or **Always allow**. Without an answer the agent gets a refusal
after 2 minutes. Time-limited permissions live until they expire or the app quits; **Settings → AI agents (MCP)**
lists them with **Revoke**. Agents can’t accept unknown or changed certificates and don’t ask for passwords: connect
once by hand first.

**What you see.** Agent sessions run without a window, with a 1280×800 desktop unless the agent asks for another
size; the overview marks them with a badge and a **Watch** button that opens the session as a tab. **Take over** in a
watched session pauses the agent (its tools answer “paused by the user”) until you click **Resume agent**. An agent
can also join a session you already have open, and then works in it while you watch. Agent sessions have their own
clipboard, separate from your Mac’s, and close when the agent disconnects or after 30 minutes without activity.

**Tools.** `list_connections`, `list_sessions`, `connect`, `disconnect`, `screenshot`, `click`, `mouse_move`,
`mouse_down`, `mouse_up`, `drag`, `scroll`, `type_text`, `key`, `clipboard_get`, `clipboard_set`, `wait`,
`wait_for_screen`, `read_screen` and `find_text` (on-device text recognition that returns click points). Coordinates
are screenshot pixels; screenshots are scaled to at most 1280 pixels on the long side.

**Audit log.** Every call is written to `~/Library/Logs/Weitblick Remote/agent.log` with time, agent, connection,
tool, arguments and result. Typed and pasted text is logged by length only.

**Limits.** RDP connections only (VNC entries are refused). `type_text` types about 40 characters per second and at
most 5,000 characters; paste longer text with `clipboard_set` and `key ctrl+v`. Text recognition reads German and
English. Error messages from the connection itself (such as “host unreachable”) come in the app’s interface
language.

## Privacy

No telemetry, no analytics, no account, no cloud. The app only connects to the computers you add. Connections are
stored in `~/Library/Application Support/Weitblick Remote/connections.json`, passwords in the login Keychain (this
Mac only, not synced). AI agents can only reach the app when you turn them on, and only from your own user account.

## Known limits

No RD Gateway, multi-monitor or H.264 yet; sign-in with user name and password only (no Kerberos, smart cards or
Entra ID); files via copy and paste, not drag and drop; Mac printers need the “Microsoft Print to PDF” driver on the
PC; read-only shared folders are not supported; Apple Silicon only. VNC entries open macOS Screen Sharing.

## Building from source

```sh
git clone --recurse-submodules https://github.com/mneuhaus/weitblick-remote.git
cd weitblick-remote
brew install cmake ninja xcodegen openssl@3
scripts/build.sh Release
```

Needs Xcode 26. Release packaging (DMG, ZIP, signing) is described in [docs/RELEASING.md](docs/RELEASING.md); the
design and its history are in [docs/SPEC.md](docs/SPEC.md) and [docs/worklog/](docs/worklog/).

## Credits

- [FreeRDP](https://www.freerdp.com/) – the RDP protocol implementation (Apache License 2.0)
- [OpenSSL](https://www.openssl.org/) – TLS and cryptography (Apache License 2.0)
- Full list and license texts: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

Windows and Remote Desktop are trademarks of Microsoft; Jump Desktop is a product of Phase Five Systems. This project
is not affiliated with either.

## License

[Apache License 2.0](LICENSE). © 2026 Marc Neuhaus.
