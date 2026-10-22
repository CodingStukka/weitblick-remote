"""The Unix domain socket weitblick-mcp connects to ($XDG_RUNTIME_DIR/weitblick-remote/mcp.sock). Port
of AgentSocketServer.swift for the GLib main loop. Only this user may connect: the directory is 0700,
the socket 0600, and every peer's uid is checked (SO_PEERCRED). Lines arrive on the main loop;
replies are written by a thread per connection (a screenshot is larger than the socket buffer, and a
peer that stops reading must not stall the app).

    server = AgentSocketServer(on_connect=lambda connection: connection.start(on_line, on_close))
    server.start()      # raises SocketServerError
    ...
    server.stop()       # closes the listener and removes the socket file
"""

from __future__ import annotations

import errno
import logging
import os
import queue
import socket
import stat
import struct
import threading
from pathlib import Path
from typing import Callable

from gi.repository import GLib

from .. import paths
from .line_framer import LineFramer, LineTooLong

log = logging.getLogger(__name__)

#: sockaddr_un.sun_path on Linux, including the terminating NUL.
_SUN_PATH_SIZE = 108


class SocketServerError(Exception):
    pass


class AgentSocketConnection:
    """One MCP client on the socket: newline-delimited JSON in both directions."""

    def __init__(self, connection_id: int, sock: socket.socket):
        self.id = connection_id
        self._socket = sock
        self._framer = LineFramer()
        self._watch: int | None = None
        self._closed = False
        self._lock = threading.Lock()
        self._outgoing: queue.Queue[bytes | None] = queue.Queue()
        self._writer = threading.Thread(target=self._write_loop, name=f"weitblick-mcp-client-{connection_id}",
                                        daemon=True)
        self._on_close: Callable[[], None] | None = None

    @property
    def is_closed(self) -> bool:
        with self._lock:
            return self._closed

    def start(self, on_line: Callable[[str], None], on_close: Callable[[], None]) -> None:
        """Starts reading: `on_line` gets every line and `on_close` runs once when the peer is gone or
        the connection was closed, both on the main loop."""
        self._on_close = on_close
        self._writer.start()

        def readable(_fd, condition) -> bool:
            if condition & (GLib.IO_ERR | GLib.IO_NVAL):
                self._finish()
                return GLib.SOURCE_REMOVE
            try:
                data = self._socket.recv(65536, socket.MSG_DONTWAIT)
            except BlockingIOError:
                return GLib.SOURCE_CONTINUE
            except InterruptedError:
                return GLib.SOURCE_CONTINUE
            except OSError:
                data = b""
            if not data:
                self._finish()
                return GLib.SOURCE_REMOVE
            try:
                lines = self._framer.append(data)
            except LineTooLong as error:
                log.warning("MCP client %d sent %s", self.id, error)
                self._finish()
                return GLib.SOURCE_REMOVE
            for line in lines:
                if self.is_closed:
                    break
                on_line(line)
            return GLib.SOURCE_CONTINUE

        self._watch = GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, self._socket.fileno(),
                                            GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, readable)

    def send(self, line: str) -> None:
        """Sends one line (a newline is added). Thread safe; dropped once closed."""
        if not self.is_closed:
            self._outgoing.put(line.encode("utf-8") + b"\n")

    def close(self) -> None:
        """Closes the connection; the peer sees end of file. on_close follows on the main loop."""
        self._finish()

    def _finish(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if self._watch is not None:
            # From inside the watch callback the SOURCE_REMOVE return value removes it as well.
            if GLib.MainContext.default().find_source_by_id(self._watch):
                GLib.source_remove(self._watch)
            self._watch = None
        try:
            # Ends a write blocked on a peer that stopped reading; the writer thread closes the fd.
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._outgoing.put(None)
        if not self._writer.is_alive() and self._writer.ident is None:
            self._socket.close()
        on_close, self._on_close = self._on_close, None
        if on_close is not None:
            GLib.idle_add(lambda: (on_close(), GLib.SOURCE_REMOVE)[1])

    def _write_loop(self) -> None:
        while True:
            data = self._outgoing.get()
            if data is None or self.is_closed:
                break
            try:
                self._socket.sendall(data)
            except OSError:
                GLib.idle_add(lambda: (self._finish(), GLib.SOURCE_REMOVE)[1])
                break
        self._socket.close()


class AgentSocketServer:
    def __init__(self, on_connect: Callable[[AgentSocketConnection], None], path: str | Path | None = None):
        self.path = str(path) if path else str(paths.mcp_socket_path())
        #: A new peer of this user (called on the main loop). Call connection.start() to read from it.
        self.on_connect = on_connect
        self._listener: socket.socket | None = None
        self._watch: int | None = None
        self._next_id = 1

    @property
    def is_running(self) -> bool:
        return self._listener is not None

    def start(self) -> None:
        if self._listener is not None:
            return
        if len(os.fsencode(self.path)) >= _SUN_PATH_SIZE:
            raise SocketServerError(f"socket path too long: {self.path}")
        directory = os.path.dirname(self.path)
        try:
            if directory:
                os.makedirs(directory, mode=0o700, exist_ok=True)
                info = os.stat(directory)
                if info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) & 0o077:
                    os.chmod(directory, 0o700)
        except OSError as error:
            raise SocketServerError(f"can't create {directory}: {error.strerror}") from error
        if os.path.lexists(self.path):
            # A live socket belongs to another instance (keep it); a dead one is left over.
            if self._is_served():
                raise SocketServerError(f"another instance serves {self.path}")
            try:
                os.unlink(self.path)
            except OSError as error:
                raise SocketServerError(f"can't remove the stale socket {self.path}: {error.strerror}") from error
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # Created 0600 from the start: no window in which another user could connect.
            previous = os.umask(0o177)
            try:
                listener.bind(self.path)
            finally:
                os.umask(previous)
            os.chmod(self.path, 0o600)
            listener.listen(8)
            listener.setblocking(False)
        except OSError as error:
            listener.close()
            if os.path.exists(self.path):
                os.unlink(self.path)
            raise SocketServerError(f"can't listen on {self.path}: {error.strerror}") from error
        self._listener = listener
        self._watch = GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, listener.fileno(), GLib.IO_IN,
                                            lambda _fd, _condition: self._accept_pending())
        log.info("MCP socket listening at %s", self.path)

    def stop(self) -> None:
        """Closes the listener and removes the socket file (open connections are closed by their owner)."""
        if self._listener is None:
            return
        if self._watch is not None and GLib.MainContext.default().find_source_by_id(self._watch):
            GLib.source_remove(self._watch)
        self._watch = None
        self._listener.close()
        self._listener = None
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        log.info("MCP socket closed")

    def _accept_pending(self) -> bool:
        if self._listener is None:
            return GLib.SOURCE_REMOVE
        while True:
            try:
                sock, _address = self._listener.accept()
            except (BlockingIOError, InterruptedError):
                return GLib.SOURCE_CONTINUE
            except OSError as error:
                if error.errno in (errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM):
                    log.error("accepting an MCP client failed: %s", error)
                return GLib.SOURCE_CONTINUE
            uid = self._peer_uid(sock)
            if uid != os.getuid():
                log.error("refused an MCP peer of uid %s", uid)
                sock.close()
                continue
            # Accepted sockets are blocking: replies are written by the connection's own thread.
            sock.setblocking(True)
            connection = AgentSocketConnection(self._next_id, sock)
            self._next_id += 1
            self.on_connect(connection)

    @staticmethod
    def _peer_uid(sock: socket.socket) -> int | None:
        try:
            credentials = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        except OSError:
            return None
        _pid, uid, _gid = struct.unpack("3i", credentials)
        return uid

    def _is_served(self) -> bool:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(1)
            probe.connect(self.path)
            return True
        except OSError:
            return False
        finally:
            probe.close()
