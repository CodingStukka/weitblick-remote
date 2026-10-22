"""weitblick-mcp: the MCP server Claude Code and other clients start (stdio). It relays stdin and stdout
to the Unix socket Weitblick Remote serves while Settings → "AI agents (MCP)" is on, and starts the
app in the background when it isn't running. All MCP logic lives in the app. Port of
Sources/WeitblickMCP/main.swift.

    claude mcp add weitblick -- weitblick-mcp
    claude mcp add weitblick -- python3 -m weitblick.mcp_helper      # from a source checkout

    weitblick-mcp [--socket <path>]

Environment: WEITBLICK_MCP_SOCKET (socket path), WEITBLICK_MCP_APP (an executable started instead of
the app, for tests; it gets --background).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from . import APP_ID, paths

SETTINGS_KEY = "agentControlEnabled"
LAUNCH_TIMEOUT = 30.0
TURNED_OFF_MESSAGE = ("AI agents are turned off in Weitblick Remote. Turn them on in Weitblick Remote → Settings → "
                      "AI agents (MCP).")
NOT_SERVING_MESSAGE = ("Weitblick Remote runs but doesn't serve agents. Check Settings → AI agents (MCP) in "
                       "Weitblick Remote.")
USAGE = """weitblick-mcp: MCP server (stdio) for Weitblick Remote's agent control.
Usage: weitblick-mcp [--socket <path>]
Set up Claude Code: claude mcp add weitblick -- weitblick-mcp"""


def print_error(message: str) -> None:
    sys.stderr.write(f"weitblick-mcp: {message}\n")
    sys.stderr.flush()


def socket_path(arguments: list[str]) -> str:
    if "--socket" in arguments:
        index = arguments.index("--socket")
        if index + 1 < len(arguments):
            return arguments[index + 1]
    return str(paths.mcp_socket_path())  # honors WEITBLICK_MCP_SOCKET


def connect_socket(path: str) -> socket.socket | None:
    """A connected socket, or None if nobody listens."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(path)
        return sock
    except OSError:
        sock.close()
        return None


def agents_enabled() -> bool:
    """The master switch as the app stores it (settings.json)."""
    try:
        with open(paths.settings_file(), encoding="utf-8") as handle:
            settings = json.load(handle)
    except (OSError, ValueError):
        return False
    return isinstance(settings, dict) and settings.get(SETTINGS_KEY) is True


def serve_error(message: str) -> None:
    """No app to talk to: answer every request with the reason until the client gives up."""
    print_error(message)
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if not isinstance(request, dict) or "method" not in request or request.get("id") is None:
            continue
        reply = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32000, "message": message}}
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    sys.exit(1)


def app_is_running() -> bool:
    """Whether the app owns its D-Bus name (a running Gtk.Application). False if D-Bus can't tell."""
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                              "NameHasOwner", GLib.Variant("(s)", (APP_ID,)), GLib.VariantType("(b)"),
                              Gio.DBusCallFlags.NONE, 2000, None)
        return bool(reply.unpack()[0])
    except Exception:  # noqa: BLE001 - no session bus, no PyGObject: assume not running
        return False


def launch_app(path: str) -> Callable[[], bool] | None:
    """Starts the app for agents (in the background, without a window); returns a check whether it
    still runs."""
    environment = dict(os.environ)
    environment["WEITBLICK_MCP_SOCKET"] = path
    executable = os.environ.get("WEITBLICK_MCP_APP")
    if executable:
        command = [executable, "--background"]
    else:
        if app_is_running():
            # Running without the socket: never start it again (that would show its window).
            return app_is_running
        command = [sys.executable, "-m", "weitblick", "--background"]
        # The package must be importable from wherever the client started this helper.
        package_root = str(Path(__file__).resolve().parent.parent)
        environment["PYTHONPATH"] = os.pathsep.join(
            [package_root] + [p for p in environment.get("PYTHONPATH", "").split(os.pathsep) if p])
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True, env=environment,
                                   cwd=str(Path.home()))
    except OSError as error:
        print_error(f"couldn't start {command[0]}: {error}")
        return None

    def still_running() -> bool:
        # A second instance hands its arguments to the running one over D-Bus and exits at once.
        return process.poll() is None or (not executable and app_is_running())
    return still_running


def wait_for_socket(path: str, still_running: Callable[[], bool]) -> socket.socket | None:
    deadline = time.monotonic() + LAUNCH_TIMEOUT
    while time.monotonic() < deadline:
        sock = connect_socket(path)
        if sock is not None:
            return sock
        if not still_running():
            return connect_socket(path)
        time.sleep(0.2)
    return None


def _write_all(fd: int, data: bytes) -> bool:
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        except OSError:
            return False
        if written <= 0:
            return False
        view = view[written:]
    return True


def relay(sock: socket.socket) -> None:
    """Copies stdin to the socket and the socket to stdout until either side ends."""
    def upstream() -> None:
        while True:
            try:
                data = os.read(0, 65536)
            except InterruptedError:
                continue
            except OSError:
                break
            if not data:
                break
            try:
                sock.sendall(data)
            except OSError:
                break
        # The client is done: the app sees end of file, cleans up and closes; don't wait forever.
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        time.sleep(3)
        os._exit(0)

    threading.Thread(target=upstream, daemon=True).start()
    while True:
        try:
            data = sock.recv(65536)
        except InterruptedError:
            continue
        except OSError:
            break
        if not data or not _write_all(1, data):
            break
    os._exit(0)


def main(arguments: list[str] | None = None) -> None:
    arguments = sys.argv[1:] if arguments is None else arguments
    if "--help" in arguments or "-h" in arguments:
        print(USAGE)
        return
    path = socket_path(arguments)
    sock = connect_socket(path)
    if sock is not None:
        relay(sock)
    # Nobody listens. With agents turned off the app wouldn't serve MCP, so don't start it.
    if not os.environ.get("WEITBLICK_MCP_APP") and not agents_enabled():
        serve_error(TURNED_OFF_MESSAGE)
    still_running = launch_app(path)
    if still_running is None:
        serve_error("Weitblick Remote couldn't be started.")
    sock = wait_for_socket(path, still_running)
    if sock is None:
        serve_error(NOT_SERVING_MESSAGE if still_running() else TURNED_OFF_MESSAGE)
    relay(sock)


if __name__ == "__main__":
    main()
