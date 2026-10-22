#!/usr/bin/env python3
"""MCP end-to-end check for agent control, against a running RDP test server (development only).

Speaks MCP (newline-delimited JSON-RPC) to the real weitblick-mcp helper (`python3 -m
weitblick.mcp_helper`), which starts the app in the background (`--background`) and relays to its
socket. Everything runs isolated: temporary XDG directories (connections, settings, audit log), a
private D-Bus session without any services (no keyring, so saved passwords are missing on purpose),
and a hidden X display. The connection to the test server has NLA off, so no password is needed.

    scripts/dev-test-server.sh &                     # 127.0.0.1:33890, any credentials
    scripts/mcp-e2e.py [--port 33890] [--display :98] [--out build/mcp-e2e]

The first failing check stops the run with exit status 1. Screenshots and a log land in --out.
"""

import argparse
import base64
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TYPED = "Grüße ÄÖÜ ß € @ {} | e2e"
PASTED = "Clipboard round trip: Straße 42 · ok"

BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:tmpdir=/tmp</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


class Failure(Exception):
    pass


# MARK: - MCP client over the helper's stdio

class Client:
    def __init__(self, env, log):
        self.log = log
        self.process = subprocess.Popen([sys.executable, "-m", "weitblick.mcp_helper"], cwd=ROOT, env=env,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.next_id = 1
        self.buffer = b""

    def send(self, message):
        line = json.dumps(message, ensure_ascii=False)
        self.log.write("> " + redact(line) + "\n")
        self.process.stdin.write(line.encode() + b"\n")
        self.process.stdin.flush()

    def read_line(self, timeout):
        deadline = time.time() + timeout
        while b"\n" not in self.buffer:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise Failure(f"no answer within {timeout:.0f} s")
            ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(self.process.stdout.fileno(), 1 << 20)
            if not chunk:
                err = self.process.stderr.read().decode(errors="replace")
                raise Failure(f"helper closed stdout (stderr: {err.strip()})")
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return line.decode()

    def request(self, method, params=None, timeout=60):
        request_id = self.next_id
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        while True:
            line = self.read_line(timeout)
            self.log.write("< " + summarize(line) + "\n")
            reply = json.loads(line)
            if reply.get("id") == request_id:
                if "error" in reply:
                    raise Failure(f"{method}: JSON-RPC error {reply['error']}")
                return reply["result"]

    def notify(self, method):
        self.send({"jsonrpc": "2.0", "method": method})

    def call(self, name, arguments, timeout=60):
        started = time.time()
        result = self.request("tools/call", {"name": name, "arguments": arguments}, timeout)
        text = "\n".join(block["text"] for block in result["content"] if block["type"] == "text")
        images = [block for block in result["content"] if block["type"] == "image"]
        self.log.write(f"  {name} {'ERROR ' if result.get('isError') else ''}({time.time() - started:.1f} s): "
                       f"{text[:400]!r}\n")
        self.log.flush()
        return bool(result.get("isError")), text, images

    def close(self):
        try:
            self.process.stdin.close()
        except BrokenPipeError:
            pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


def redact(line):
    try:
        message = json.loads(line)
        arguments = message.get("params", {}).get("arguments", {})
        if "text" in arguments:
            arguments["text"] = f"<{len(arguments['text'])} characters>"
        return json.dumps(message, ensure_ascii=False)
    except (ValueError, AttributeError):
        return line


def summarize(line):
    return line if len(line) < 600 else line[:300] + f"… ({len(line)} bytes)"


def png_is_not_blank(data):
    """Distinct byte values of the decompressed scanlines: a blank screen has a handful."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return True  # JPEG: only used for busy screens
    position, idat = 8, b""
    while position < len(data):
        length = int.from_bytes(data[position:position + 4], "big")
        kind = data[position + 4:position + 8]
        if kind == b"IDAT":
            idat += data[position + 8:position + 8 + length]
        position += 12 + length
    raw = zlib.decompress(idat)
    return len(set(raw[::7])) > 40


# MARK: - Isolated environment

def isolated_environment(work: Path, display: str) -> dict:
    env = dict(os.environ)
    for name in ("CONFIG", "DATA", "STATE", "CACHE"):
        directory = work / name.lower()
        directory.mkdir(parents=True, exist_ok=True)
        env[f"XDG_{name}_HOME"] = str(directory)
    env.update({
        "WEITBLICK_MCP_SOCKET": str(work / "mcp.sock"),
        "DISPLAY": display,
        "GDK_BACKEND": "x11",
        "GSETTINGS_BACKEND": "memory",
        "NO_AT_BRIDGE": "1",
        "GTK_A11Y": "none",
        "PYTHONPATH": str(ROOT),
        "WB_E2E_MARKER": str(work),
        # Test hooks of the agent host (weitblick/agent/host.py).
        "WEITBLICK_AGENT_PROMPT_ANSWERS": "deny,once,ten-minutes,deny",
        "WEITBLICK_AGENT_PROMPT_TIMEOUT": "6",
        "WEITBLICK_AGENT_GRANT_SECONDS": "8",
        "WEITBLICK_AGENT_IDLE_QUIT": "20",
    })
    return env


def probe_fingerprint(host: str, port: int, state_dir: str) -> str:
    """Connects once with the bridge and reads the server's certificate fingerprint."""
    sys.path.insert(0, str(ROOT))
    from gi.repository import GLib
    from weitblick.rdp.session import CertificateDecision, RDPSession, SessionConfig
    main = GLib.MainLoop()
    found = []
    session = RDPSession(SessionConfig(host=host, port=port, username="probe", password="probe", nla=False,
                                       clipboard=False, auto_reconnect=False, state_directory=state_dir))

    def certificate(cert, reply):
        found.append(cert.fingerprint)
        reply(CertificateDecision.REJECT)

    session.certificate_handler = certificate
    session.connect("disconnected", lambda *_: main.quit())
    session.start()
    GLib.timeout_add_seconds(20, main.quit)
    main.run()
    session.close()
    if not found:
        raise Failure(f"no certificate from {host}:{port} (is scripts/dev-test-server.sh running?)")
    return found[0]


def write_store(env: dict, host: str, port: int, fingerprint: str) -> None:
    saved = {key: os.environ.get(key) for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")}
    os.environ.update({key: env[key] for key in saved})
    try:
        from weitblick.store import AgentAccess, Connection, ConnectionStore
        from weitblick.store.settings import AppSettings

        def make(name, access, trusted=True, nla_off=True):
            connection = Connection(name=name, host=host, port=port, username="agent")
            connection.security.agent_access = access
            connection.security.disable_nla = nla_off
            if trusted:
                connection.security.trusted_certificate_fingerprints = [fingerprint]
            return connection

        ConnectionStore().replace_all([
            make("Agent VM", AgentAccess.ALWAYS),
            make("Ask VM", AgentAccess.ASK),
            make("Never VM", AgentAccess.NEVER),
            make("Untrusted VM", AgentAccess.ALWAYS, trusted=False),
            make("No password VM", AgentAccess.ALWAYS, nla_off=False),
        ])
        AppSettings().agent_control_enabled = True
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def app_pids(marker: str) -> list[int]:
    """Processes started with our marker in their environment (the background app)."""
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            environ = (entry / "environ").read_bytes()
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if f"WB_E2E_MARKER={marker}".encode() in environ and b"--background" in cmdline:
            pids.append(int(entry.name))
    return pids


# MARK: - The run

class Run:
    def __init__(self, args, env, work: Path):
        self.args = args
        self.env = env
        self.work = work
        self.out = Path(args.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.log = open(self.out / "run.log", "w")
        self.client = Client(env, self.log)
        self.shots = 0
        self.skipped = []

    def check(self, condition, message):
        self.log.write(("  ok: " if condition else "  FAIL: ") + message + "\n")
        self.log.flush()
        print(("  ok   " if condition else "  FAIL ") + message, flush=True)
        if not condition:
            raise Failure(message)

    def skip(self, message):
        self.skipped.append(message)
        self.log.write("  skip: " + message + "\n")
        print("  skip " + message, flush=True)

    def ok(self, name, arguments, timeout=60):
        error, text, images = self.client.call(name, arguments, timeout)
        if error:
            raise Failure(f"{name} failed: {text}")
        return text, images

    def save(self, images, label):
        for image in images:
            self.shots += 1
            extension = "png" if image["mimeType"] == "image/png" else "jpg"
            (self.out / f"{self.shots:02d}-{label}.{extension}").write_bytes(base64.b64decode(image["data"]))

    @staticmethod
    def session_id(text):
        for word in text.replace(".", " ").split():
            if word.startswith("s") and word[1:].isdigit():
                return word
        raise Failure(f"no session id in {text!r}")

    def protocol(self):
        result = self.client.request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "mcp-e2e", "version": "linux"},
        }, timeout=60)
        self.client.notify("notifications/initialized")
        self.check(result["serverInfo"]["name"] == "weitblick-remote", "initialize: server weitblick-remote "
                   f"{result['serverInfo'].get('version')}, protocol {result['protocolVersion']}")
        self.check("take a screenshot first" in result.get("instructions", "").lower(),
                   "instructions tell the model to take a screenshot first")
        tools = [tool["name"] for tool in self.client.request("tools/list", {})["tools"]]
        expected = {"list_connections", "list_sessions", "connect", "disconnect", "screenshot", "click",
                    "mouse_move", "mouse_down", "mouse_up", "drag", "scroll", "type_text", "key",
                    "clipboard_get", "clipboard_set", "wait", "wait_for_screen", "read_screen", "find_text"}
        self.check(set(tools) == expected, f"tools/list: {len(tools)} tools")
        text, _ = self.ok("list_connections", {})
        for name in ("Agent VM", "Ask VM", "Never VM", "Untrusted VM", "No password VM"):
            self.check(f'"{name}"' in text, f"list_connections lists {name}")

    def refusals(self):
        error, text, _ = self.client.call("connect", {"connection": "Never VM"})
        self.check(error and "Never" in text and "Security" in text,
                   "Never: refused, and the message says where to change it")
        error, text, _ = self.client.call("connect", {"connection": "No password VM"})
        self.check(error and "connect once by hand" in text, "missing password: refused, connect once by hand")
        error, text, _ = self.client.call("connect", {"connection": "Untrusted VM"}, timeout=120)
        self.check(error and "certificate" in text.lower() and "by hand" in text,
                   "unknown certificate: refused, connect once by hand")
        error, text, _ = self.client.call("screenshot", {"session": "s999"})
        self.check(error and "Unknown session" in text, "unknown session id: refused")

    def desktop(self):
        text, _ = self.ok("connect", {"connection": "Agent VM"}, timeout=150)
        session = self.session_id(text)
        self.check("1280×800" in text, f"connect: {session}, 1280×800 desktop")
        text, _ = self.ok("list_sessions", {})
        self.check(f'"{session}"' in text and '"owner":"you"' in text.replace(" ", ""),
                   "list_sessions shows the session as yours")
        error, text, _ = self.client.call("connect", {"connection": "Agent VM"})
        self.check(not error and "Already connected" in text, "connect again: already connected")
        self.ok("wait_for_screen", {"session": session, "timeout": 20, "settle_ms": 1000}, timeout=40)
        text, images = self.ok("screenshot", {"session": session})
        self.save(images, "desktop")
        data = base64.b64decode(images[0]["data"])
        self.check(len(images) == 1 and png_is_not_blank(data),
                   f"screenshot: {images[0]['mimeType']}, {len(data)} bytes, not blank")
        self.check("1280×800" in text and "scale 1" in text, "screenshot states size and scale")
        text, images = self.ok("screenshot", {"session": session,
                                              "region": {"x": 100, "y": 100, "width": 300, "height": 200}})
        self.check("Region of the screen: 300×200 px" in text, "screenshot of a region states its size")

        # The test server shows a text editor: click into it, clear it, type, read it back.
        editor = {"x": 500, "y": 400}
        self.ok("click", {"session": session, **editor})
        self.ok("key", {"session": session, "keys": "ctrl+a delete"})
        self.ok("wait_for_screen", {"session": session, "timeout": 5}, timeout=15)
        region = {"x": 0, "y": 60, "width": 1000, "height": 200}
        _, before = self.ok("screenshot", {"session": session, "region": region})
        # FreeRDP's shadow server ignores Unicode key events ("not implemented" in its log), Windows
        # doesn't: here only the line break (Enter, a scancode) of the typed text shows.
        text, images = self.ok("type_text", {"session": session, "text": TYPED + "\n", "screenshot": True},
                               timeout=40)
        self.save(images, "typed")
        self.check(bool(images) and ("settled" in text or "changing" in text),
                   "type_text with screenshot: true settles and returns a screenshot")
        _, after = self.ok("screenshot", {"session": session, "region": region})
        self.check(before[0]["data"] != after[0]["data"], "type_text reached the session (Enter moved the caret)")
        self.ok("key", {"session": session, "keys": "h e l l o shift+1"})
        self.ok("wait_for_screen", {"session": session, "timeout": 5}, timeout=15)
        _, keyed = self.ok("screenshot", {"session": session, "region": region})
        self.save(keyed, "keyed")
        self.check(keyed[0]["data"] != after[0]["data"], "key typed letters through the session's layout")

        error, text, _ = self.client.call("read_screen", {"session": session})
        if error and "Tesseract" in text:
            self.check("apt install" in text, "read_screen without Tesseract explains how to install it")
            self.skip("text recognition (Tesseract not installed)")
        else:
            self.check(not error, f"read_screen: {text.splitlines()[0] if text else ''}")
            error, text, _ = self.client.call("find_text", {"session": session, "query": "e2e"})
            self.check(not error and not text.startswith("No match"), "find_text finds the typed text")

        self.ok("key", {"session": session, "keys": "ctrl+a ctrl+c"})
        error, text, _ = self.client.call("clipboard_get", {"session": session})
        if error and "clipboard" in text.lower():
            self.skip(f"clipboard ({text[:90]})")
        else:
            self.check(not error and text == TYPED, f"ctrl+a ctrl+c, clipboard_get returns the typed text ({text!r})")
            self.ok("clipboard_set", {"session": session, "text": PASTED})
            self.ok("key", {"session": session, "keys": "ctrl+a ctrl+v"})
            self.ok("wait_for_screen", {"session": session, "timeout": 5}, timeout=15)
            self.ok("key", {"session": session, "keys": "ctrl+a ctrl+c"})
            text, _ = self.ok("clipboard_get", {"session": session})
            self.check(text == PASTED, f"clipboard_set, ctrl+v, copied back: {text!r}")

        self.ok("key", {"session": session, "keys": "ctrl+a delete"})
        text, _ = self.ok("mouse_move", {"session": session, "x": 640, "y": 400})
        self.check("(640, 400)" in text, "mouse_move reports the point")
        self.ok("mouse_down", {"session": session, "x": 300, "y": 300})
        self.ok("mouse_up", {"session": session, "x": 320, "y": 310})
        self.ok("drag", {"session": session, "path": [[300, 300], [400, 350], [450, 360]]})
        text, _ = self.ok("scroll", {"session": session, "x": 500, "y": 400, "dy": 3})
        self.check("3 notches down" in text, "scroll reports the notches")
        error, text, _ = self.client.call("click", {"session": session, "x": 5000, "y": 10})
        self.check(error and "outside the screen" in text, "click outside the screen: refused")
        error, text, _ = self.client.call("key", {"session": session, "keys": "cmd+c"})
        self.check(error and "no Cmd key" in text, "cmd+c: refused, use ctrl")
        text, _ = self.ok("wait", {"session": session, "seconds": 0.3})
        self.check("Waited 0.3 s" in text, "wait")
        text, images = self.ok("screenshot", {"session": session})
        self.save(images, "after-input")
        self.check("Pointer at" in text, "screenshot names the agent's pointer")

        text, _ = self.ok("disconnect", {"session": session})
        self.check("Disconnected" in text, f"disconnect {session}")
        error, text, _ = self.client.call("screenshot", {"session": session})
        self.check(error, "the session is gone after disconnect")

    def gate(self):
        error, text, _ = self.client.call("connect", {"connection": "Ask VM"}, timeout=60)
        self.check(error and "denied" in text.lower(), f"Ask + Deny: refused ({text[:80]!r})")

        text, _ = self.ok("connect", {"connection": "Ask VM"}, timeout=90)
        session = self.session_id(text)
        self.check(True, f"Ask + Allow once: connected ({session})")
        self.ok("screenshot", {"session": session})
        self.ok("disconnect", {"session": session})

        text, _ = self.ok("connect", {"connection": "Ask VM"}, timeout=90)
        session = self.session_id(text)
        granted = time.time()
        self.check(True, f"Ask + Allow for 10 minutes (8 s in this test): connected ({session})")
        self.ok("screenshot", {"session": session})
        time.sleep(max(0, 9 - (time.time() - granted)))
        error, text, _ = self.client.call("screenshot", {"session": session}, timeout=60)
        self.check(error and "denied" in text.lower(),
                   "the grant expired: the next call asked again (answered Deny, refused)")
        self.ok("disconnect", {"session": session})

        started = time.time()
        error, text, _ = self.client.call("connect", {"connection": "Ask VM"}, timeout=60)
        waited = time.time() - started
        self.check(error and "no answer from the user" in text.lower() and waited >= 5,
                   f"Ask without an answer: fails after {waited:.0f} s with “no answer from the user”")

    def watched(self):
        # A new client after the quit: the helper starts the app again.
        self.client = Client(self.env, self.log)
        self.client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "mcp-e2e", "version": "watch"}}, timeout=60)
        self.client.notify("notifications/initialized")
        self.check(True, "a new client starts the app again")
        text, _ = self.ok("connect", {"connection": "Agent VM", "show": True}, timeout=150)
        session = self.session_id(text)
        self.check(True, f"connect with show: true opens a tab ({session})")
        text, images = self.ok("screenshot", {"session": session})
        self.save(images, "watched")
        data = base64.b64decode(images[0]["data"])
        self.check(png_is_not_blank(data), "the watched session has a picture")
        text, _ = self.ok("disconnect", {"session": session})
        self.check("Disconnected" in text, f"disconnect {session} closes its tab")
        # The window stays (an app that showed its interface doesn't quit by itself).
        self.client.close()
        for pid in app_pids(str(self.work)):
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass

    def audit(self):
        log = (Path(self.env["XDG_STATE_HOME"]) / "weitblick-remote" / "agent.log").read_text()
        self.check(f"<{len(TYPED) + 1} characters>" in log and TYPED not in log,
                   "audit log: typed text logged by length only")
        opened = log.count("opened: client=\"mcp-e2e")
        self.check(opened >= 4, f"audit log: {opened} permission prompts opened")
        self.check("tool=connect" in log and "tool=screenshot" in log, "audit log: tool calls recorded")

    def finish(self):
        pids = app_pids(str(self.work))
        self.client.close()
        if not pids:
            self.skip("background app pid not found")
            return
        deadline = time.time() + 60
        while time.time() < deadline:
            if not any(Path(f"/proc/{pid}").exists() for pid in pids):
                self.check(True, "the background app quit by itself after the client left")
                return
            time.sleep(1)
        self.check(False, "the background app quit by itself after the client left")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=33890)
    parser.add_argument("--display", default=":98")
    parser.add_argument("--out", default=str(ROOT / "build" / "mcp-e2e"))
    args = parser.parse_args()

    if not os.environ.get("WB_E2E_PRIVATE_BUS"):
        # A bus of our own: no keyring, no second copy of the user's running app.
        config = Path(tempfile.mkdtemp(prefix="wb-e2e-bus-")) / "session.conf"
        config.write_text(BUS_CONFIG)
        env = dict(os.environ, WB_E2E_PRIVATE_BUS="1")
        try:
            return subprocess.call(["dbus-run-session", f"--config-file={config}", "--", sys.executable,
                                    *sys.argv], env=env)
        finally:
            shutil.rmtree(config.parent, ignore_errors=True)

    work = Path(tempfile.mkdtemp(prefix="wb-e2e-"))
    env = isolated_environment(work, args.display)
    fingerprint = probe_fingerprint(args.host, args.port, str(work / "probe-state"))
    write_store(env, args.host, args.port, fingerprint)
    print(f"MCP e2e in {work} against {args.host}:{args.port} (certificate {fingerprint[:23]}…)", flush=True)
    run = Run(args, env, work)
    try:
        run.protocol()
        run.refusals()
        run.desktop()
        run.gate()
        run.audit()
        run.finish()
        run.watched()
    except Failure as failure:
        print(f"MCP-E2E FAIL: {failure}", flush=True)
        run.log.write(f"FAIL: {failure}\n")
        run.client.close()
        for pid in app_pids(str(work)):
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass
        return 1
    skipped = f" ({len(run.skipped)} skipped: {'; '.join(run.skipped)})" if run.skipped else ""
    print(f"MCP-E2E PASS{skipped}", flush=True)
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
