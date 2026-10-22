import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

from gi.repository import GLib

from weitblick import paths
from weitblick.agent.socket_server import AgentSocketServer, SocketServerError

PROJECT_ROOT = str(paths.SOURCE_ROOT)


def run_until(predicate, timeout=5.0):
    """Iterates the default main context until predicate() holds."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        context.iteration(False)
        time.sleep(0.005)


class EchoServer:
    """A socket server that answers every line with "echo:<line>"."""

    def __init__(self, path):
        self.lines = []
        self.closed = []
        self.connections = []
        self.server = AgentSocketServer(self._connected, path)

    def _connected(self, connection):
        self.connections.append(connection)

        def on_line(line):
            self.lines.append(line)
            connection.send("echo:" + line)

        connection.start(on_line, lambda: self.closed.append(connection.id))


class SocketServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="wb-")
        self.path = os.path.join(self.directory, "run", "mcp.sock")

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.directory])

    def test_lines_go_both_ways_and_the_socket_is_private(self):
        echo = EchoServer(self.path)
        echo.server.start()
        try:
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.path)).st_mode), 0o700)
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(self.path)
            client.sendall('{"a":1}\r\n{"b":"Grüße"}\n'.encode())
            run_until(lambda: len(echo.lines) == 2)
            self.assertEqual(echo.lines, ['{"a":1}', '{"b":"Grüße"}'])
            received = b""
            client.settimeout(5)
            while received.count(b"\n") < 2:
                received += client.recv(4096)
            self.assertEqual(received.decode(), 'echo:{"a":1}\necho:{"b":"Grüße"}\n')
            # A large reply (like a screenshot) arrives whole.
            echo.connections[0].send("x" * 3_000_000)
            data = b""
            while not data.endswith(b"\n"):
                data += client.recv(1 << 20)
            self.assertEqual(len(data), 3_000_001)
            client.close()
            run_until(lambda: echo.closed == [1])
        finally:
            echo.server.stop()
        self.assertFalse(os.path.exists(self.path))

    def test_server_side_close_ends_the_peer(self):
        echo = EchoServer(self.path)
        echo.server.start()
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(self.path)
            run_until(lambda: echo.connections)
            echo.connections[0].close()
            client.settimeout(5)
            self.assertEqual(client.recv(10), b"")
            client.close()
            run_until(lambda: echo.closed == [1])
            echo.connections[0].send("dropped")  # no error once closed
        finally:
            echo.server.stop()

    def test_a_live_socket_is_kept_and_a_stale_one_replaced(self):
        first = EchoServer(self.path)
        first.server.start()
        try:
            with self.assertRaises(SocketServerError):
                EchoServer(self.path).server.start()
        finally:
            first.server.stop()
        # A leftover socket file nobody listens on.
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(self.path)
        stale.close()
        second = EchoServer(self.path)
        second.server.start()
        second.server.stop()

    def test_too_long_paths_are_refused(self):
        with self.assertRaises(SocketServerError):
            AgentSocketServer(lambda c: None, "/tmp/" + "x" * 120).start()


class MCPHelperTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="wb-")
        self.path = os.path.join(self.directory, "mcp.sock")
        self.environment = dict(os.environ, PYTHONPATH=PROJECT_ROOT, XDG_CONFIG_HOME=self.directory,
                                WEITBLICK_MCP_SOCKET=self.path)
        self.environment.pop("WEITBLICK_MCP_APP", None)

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.directory])

    def helper(self, environment=None):
        return subprocess.Popen([sys.executable, "-m", "weitblick.mcp_helper"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment or self.environment)

    def test_relays_to_the_running_app(self):
        echo = EchoServer(self.path)
        echo.server.start()
        try:
            process = self.helper()
            output = {}

            def talk():
                output["stdout"], output["stderr"] = process.communicate(b'{"jsonrpc":"2.0","id":1}\n', timeout=20)

            thread = threading.Thread(target=talk)
            thread.start()
            run_until(lambda: not thread.is_alive(), timeout=20)
            self.assertEqual(echo.lines, ['{"jsonrpc":"2.0","id":1}'])
            self.assertEqual(output["stdout"], b'echo:{"jsonrpc":"2.0","id":1}\n')
            self.assertEqual(process.returncode, 0)
        finally:
            echo.server.stop()

    def test_agents_turned_off_answers_every_request(self):
        process = self.helper()
        stdout, stderr = process.communicate(
            b'{"jsonrpc":"2.0","id":7,"method":"initialize"}\n{"jsonrpc":"2.0","method":"notifications/x"}\n',
            timeout=20)
        self.assertEqual(process.returncode, 1)
        reply = json.loads(stdout.decode().strip())
        self.assertEqual(reply["id"], 7)
        self.assertEqual(reply["error"]["code"], -32000)
        self.assertIn("turned off", reply["error"]["message"])
        self.assertIn(b"turned off", stderr)

    def test_launches_the_app_when_nobody_listens(self):
        fake_app = Path(self.directory) / "fake-app"
        fake_app.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import os, socket, sys
            assert sys.argv[1:] == ["--background"], sys.argv
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(os.environ["WEITBLICK_MCP_SOCKET"])
            server.listen(1)
            connection, _ = server.accept()
            line = connection.makefile("rb").readline()
            connection.sendall(b"app:" + line)
            connection.close()
            """))
        fake_app.chmod(0o755)
        environment = dict(self.environment, WEITBLICK_MCP_APP=str(fake_app))
        process = self.helper(environment)
        stdout, _stderr = process.communicate(b'{"id":1}\n', timeout=30)
        self.assertEqual(stdout, b'app:{"id":1}\n')


if __name__ == "__main__":
    unittest.main()
