#!/usr/bin/python3
"""MCP end-to-end driver for Weitblick Remote's agent control (run by scripts/mcp-e2e.sh).

Speaks MCP (newline-delimited JSON-RPC) to the real `weitblick-mcp` helper, which launches the
hidden test app copy through WEITBLICK_MCP_APP and relays to its socket. Every step writes to the
evidence folder; the first failing check stops the run with exit status 1.

    mcp-e2e.py --helper <weitblick-mcp> --env-file <file with the helper's environment>
               --out <evidence dir> --run <n> [--grant-seconds 8] [--prompt-timeout 6]
               [--expect-quit <app pid file>]
"""
import argparse
import base64
import json
import os
import select
import subprocess
import sys
import time
import zlib

TYPED = "Grüße ÄÖÜ ß € @ {} \\ |"
PASTED = "Clipboard round trip: Straße 42 · ok"


class Failure(Exception):
    pass


class Client:
    def __init__(self, helper, env, log):
        self.log = log
        self.process = subprocess.Popen([helper], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=env)
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


class Run:
    def __init__(self, args):
        self.args = args
        self.out = args.out
        os.makedirs(self.out, exist_ok=True)
        self.log = open(os.path.join(self.out, f"run{args.run}.log"), "w")
        env = dict(os.environ)
        with open(args.env_file) as handle:
            for line in handle:
                key, _, value = line.rstrip("\n").partition("=")
                if key:
                    env[key] = value
        self.client = Client(args.helper, env, self.log)
        self.shots = 0

    def check(self, condition, message):
        self.log.write(("  ok: " if condition else "  FAIL: ") + message + "\n")
        self.log.flush()
        print(("  ok   " if condition else "  FAIL ") + message, flush=True)
        if not condition:
            raise Failure(message)

    def ok(self, name, arguments, timeout=60):
        error, text, images = self.client.call(name, arguments, timeout)
        if error:
            raise Failure(f"{name} failed: {text}")
        return text, images

    def save(self, images, label):
        for image in images:
            self.shots += 1
            extension = "png" if image["mimeType"] == "image/png" else "jpg"
            path = os.path.join(self.out, f"run{self.args.run}-{self.shots:02d}-{label}.{extension}")
            with open(path, "wb") as handle:
                handle.write(base64.b64decode(image["data"]))

    def session_id(self, text):
        for word in text.replace(".", " ").split():
            if word.startswith("s") and word[1:].isdigit():
                return word
        raise Failure(f"no session id in {text!r}")

    # MARK: steps

    def protocol(self):
        result = self.client.request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "mcp-e2e", "version": f"run{self.args.run}"},
        }, timeout=60)
        self.client.notify("notifications/initialized")
        self.check(result["serverInfo"]["name"] == "weitblick-remote", "initialize: server weitblick-remote "
                   f"{result['serverInfo'].get('version')}, protocol {result['protocolVersion']}")
        self.check("screenshot first" in result.get("instructions", "").lower()
                   or "take a screenshot first" in result.get("instructions", "").lower(),
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

    def notepad(self):
        text, _ = self.ok("connect", {"connection": "Agent VM"}, timeout=150)
        session = self.session_id(text)
        self.check("1280×800" in text, f"connect: {session}, 1280×800 desktop")
        self.ok("wait_for_screen", {"session": session, "timeout": 20, "settle_ms": 1500}, timeout=40)
        text, images = self.ok("screenshot", {"session": session})
        self.save(images, "desktop")
        data = base64.b64decode(images[0]["data"])
        self.check(len(images) == 1 and png_is_not_blank(data),
                   f"screenshot: {images[0]['mimeType']}, {len(data)} bytes, not blank")
        self.check("1280×800" in text and "scale 1" in text, "screenshot states size and scale")

        self.ok("key", {"session": session, "keys": "win+r"})
        self.ok("wait_for_screen", {"session": session, "timeout": 10}, timeout=20)
        self.ok("type_text", {"session": session, "text": "notepad"})
        self.ok("key", {"session": session, "keys": "enter"})
        text, _ = self.ok("wait_for_screen", {"session": session, "timeout": 20, "settle_ms": 1000}, timeout=40)
        found = False
        for _ in range(10):
            error, text, _ = self.client.call("find_text", {"session": session, "query": "Unbenannt"})
            if not error and "match" in text and not text.startswith("No match"):
                found = True
                break
            time.sleep(1)
        self.check(found, "Notepad opened (win+r, notepad, enter; find_text finds “Unbenannt”)")

        text, images = self.ok("type_text", {"session": session, "text": TYPED, "screenshot": True}, timeout=40)
        self.save(images, "typed")
        text, _ = self.ok("find_text", {"session": session, "query": TYPED})
        self.check(not text.startswith("No match"), f"find_text finds the typed text: {text.splitlines()[1:2]}")
        self.ok("read_screen", {"session": session, "region": {"x": 0, "y": 0, "width": 1280, "height": 400}})

        self.ok("key", {"session": session, "keys": "ctrl+a ctrl+c"})
        text, _ = self.ok("clipboard_get", {"session": session})
        self.check(text == TYPED, f"ctrl+a ctrl+c, clipboard_get returns the typed text exactly ({text!r})")

        self.ok("clipboard_set", {"session": session, "text": PASTED})
        self.ok("key", {"session": session, "keys": "ctrl+a ctrl+v"})
        self.ok("wait_for_screen", {"session": session, "timeout": 5}, timeout=15)
        self.ok("key", {"session": session, "keys": "ctrl+a ctrl+c"})
        text, _ = self.ok("clipboard_get", {"session": session})
        self.check(text == PASTED, f"clipboard_set, ctrl+v, copied back: {text!r}")
        text, images = self.ok("screenshot", {"session": session})
        self.save(images, "pasted")

        # Close without saving: Windows 11 Notepad asks only if it doesn't keep the tab itself.
        self.ok("key", {"session": session, "keys": "ctrl+w"})
        self.ok("wait_for_screen", {"session": session, "timeout": 5}, timeout=15)
        error, text, _ = self.client.call("find_text", {"session": session, "query": "Nicht speichern"})
        if not error and not text.startswith("No match"):
            line = text.splitlines()[1]
            x, y = line.rsplit("click (", 1)[1].rstrip(")").split(")")[0].split(", ")
            self.ok("click", {"session": session, "x": float(x), "y": float(y)})
            self.ok("wait_for_screen", {"session": session, "timeout": 5}, timeout=15)
        self.ok("key", {"session": session, "keys": "alt+f4"})
        self.ok("wait_for_screen", {"session": session, "timeout": 5}, timeout=15)
        error, text, _ = self.client.call("find_text", {"session": session, "query": "Nicht speichern"})
        if not error and not text.startswith("No match"):
            line = text.splitlines()[1]
            x, y = line.rsplit("click (", 1)[1].split(")")[0].split(", ")
            self.ok("click", {"session": session, "x": float(x), "y": float(y)})
            self.ok("wait_for_screen", {"session": session, "timeout": 5}, timeout=15)
        text, images = self.ok("screenshot", {"session": session})
        self.save(images, "closed")
        error, text, _ = self.client.call("find_text", {"session": session, "query": PASTED})
        self.check(text.startswith("No match"), "Notepad closed without saving")
        text, _ = self.ok("disconnect", {"session": session})
        self.check("Disconnected" in text, f"disconnect {session}")
        error, text, _ = self.client.call("screenshot", {"session": session})
        self.check(error, "the session is gone after disconnect")

    def gate(self):
        error, text, _ = self.client.call("connect", {"connection": "Ask VM"}, timeout=150)
        self.check(error and "denied" in text.lower(), f"Ask + Deny: refused ({text[:80]!r})")

        text, _ = self.ok("connect", {"connection": "Ask VM"}, timeout=150)
        session = self.session_id(text)
        self.check(True, f"Ask + Allow once: connected ({session})")
        self.ok("screenshot", {"session": session})
        self.ok("disconnect", {"session": session})

        text, _ = self.ok("connect", {"connection": "Ask VM"}, timeout=150)
        session = self.session_id(text)
        granted = time.time()
        self.check(True, f"Ask + Allow for 10 minutes ({self.args.grant_seconds} s in this test): connected ({session})")
        self.ok("screenshot", {"session": session})
        time.sleep(max(0, self.args.grant_seconds + 1 - (time.time() - granted)))
        error, text, _ = self.client.call("screenshot", {"session": session}, timeout=60)
        self.check(error and "denied" in text.lower(),
                   "the grant expired: the next call asked again (answered Deny, refused)")
        self.ok("disconnect", {"session": session})

        started = time.time()
        error, text, _ = self.client.call("connect", {"connection": "Ask VM"}, timeout=120)
        waited = time.time() - started
        self.check(error and "no answer from the user" in text.lower() and waited >= self.args.prompt_timeout - 1,
                   f"Ask without an answer: fails after {waited:.0f} s with “no answer from the user”")

    def audit(self):
        with open(os.environ["M7_AGENT_LOG"]) as handle:
            log = handle.read()
        self.check(f"<{len(TYPED)} characters>" in log and TYPED not in log, "audit log: typed text logged by length only")
        clipboard_lines = [line for line in log.splitlines() if "tool=clipboard_set" in line]
        self.check(clipboard_lines and all(PASTED not in line and f"<{len(PASTED)} characters>" in line
                                           for line in clipboard_lines),
                   "audit log: clipboard_set text logged by length only")
        opened = log.count("opened: client=\"mcp-e2e")
        self.check(opened >= 5, f"audit log: {opened} permission prompts opened")

    def finish(self):
        self.client.close()
        if self.args.expect_quit:
            with open(self.args.expect_quit) as handle:
                pid = int(handle.read().strip())
            deadline = time.time() + 100
            while time.time() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    self.check(True, "the hidden app quit by itself after the client left")
                    return
                time.sleep(2)
            self.check(False, "the hidden app quit by itself after the client left")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--helper", required=True)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--run", type=int, default=1)
    parser.add_argument("--grant-seconds", type=float, default=8)
    parser.add_argument("--prompt-timeout", type=float, default=6)
    parser.add_argument("--expect-quit")
    args = parser.parse_args()
    run = Run(args)
    try:
        run.protocol()
        run.refusals()
        run.notepad()
        run.gate()
        run.audit()
        run.finish()
    except Failure as failure:
        print(f"MCP-E2E run {args.run} FAIL: {failure}", flush=True)
        run.log.write(f"FAIL: {failure}\n")
        run.client.close()
        return 1
    print(f"MCP-E2E run {args.run} PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
