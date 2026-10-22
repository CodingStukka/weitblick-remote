"""Files over the clipboard (FileGroupDescriptorW plus FileContents streams, MS-RDPECLIP 3.1.5.4).

Local -> Windows: files copied in a file manager (text/uri-list, x-special/gnome-copied-files) are
announced as a descriptor; when the server asks for it they are listed (folders recursively) and
later read in pieces as the server asks (`LocalFileList`).

Windows -> local: a server descriptor becomes a lazy text/uri-list. When an app reads it (a paste
in Nautilus) the files are downloaded into the clipboard cache (`RemoteFileTransfer`) and their
file:// URIs handed out.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import struct
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote, unquote, urlsplit

from .. import paths
from ..rdp.session import RemoteFileRequest

log = logging.getLogger(__name__)

#: Largest total of one file copy, in either direction.
DEFAULT_MAX_TRANSFER_SIZE = 2 << 30


@dataclass(frozen=True)
class ClipboardFileEntry:
    """One file or folder of a clipboard file list."""

    #: Path relative to the copied items, components joined with "\\" like Windows writes them.
    path: str
    is_directory: bool
    size: int
    #: Modification time (seconds since 1970), if known.
    modified: float | None = None

    @property
    def components(self) -> list[str]:
        return [part for part in self.path.split("\\") if part]

    @property
    def is_top_level(self) -> bool:
        return "\\" not in self.path


# MARK: FileGroupDescriptorW

class FileGroupDescriptor:
    """The "FileGroupDescriptorW" clipboard format: a count and one 592-byte FILEDESCRIPTORW per entry."""

    RECORD_SIZE = 592
    #: cFileName holds 260 UTF-16 units including the terminator.
    MAX_PATH_LENGTH = 259

    _FD_ATTRIBUTES = 0x04
    _FD_WRITESTIME = 0x20
    _FD_FILESIZE = 0x40
    _FD_PROGRESSUI = 0x4000
    _FD_UNICODE = 0x80000000
    _DIRECTORY = 0x10  # FILE_ATTRIBUTE_DIRECTORY
    _NORMAL = 0x80  # FILE_ATTRIBUTE_NORMAL
    #: 100 ns ticks from 1601-01-01 (FILETIME epoch) to 1970-01-01.
    _EPOCH_TICKS = 11_644_473_600 * 10_000_000

    @classmethod
    def encode(cls, entries: list[ClipboardFileEntry]) -> bytes:
        """Entries with paths longer than MAX_PATH_LENGTH must be filtered out before."""
        out = bytearray(struct.pack("<I", len(entries)))
        for entry in entries:
            record = bytearray(cls.RECORD_SIZE)
            flags = cls._FD_ATTRIBUTES | cls._FD_FILESIZE | cls._FD_PROGRESSUI | cls._FD_UNICODE
            if entry.modified is not None:
                flags |= cls._FD_WRITESTIME
            struct.pack_into("<I", record, 0, flags)
            struct.pack_into("<I", record, 36, cls._DIRECTORY if entry.is_directory else cls._NORMAL)
            if entry.modified is not None:
                ticks = max(0, round(entry.modified * 10_000_000) + cls._EPOCH_TICKS)
                struct.pack_into("<Q", record, 56, ticks)
            size = 0 if entry.is_directory else entry.size
            struct.pack_into("<II", record, 64, size >> 32, size & 0xFFFFFFFF)
            name = entry.path.encode("utf-16-le", "surrogatepass")[:cls.MAX_PATH_LENGTH * 2]
            record[72:72 + len(name)] = name
            out += record
        return bytes(out)

    @classmethod
    def decode(cls, data: bytes) -> list[ClipboardFileEntry] | None:
        """The entries, or None if the data is malformed or names a path outside the copied items."""
        if len(data) < 4:
            return None
        count = struct.unpack_from("<I", data, 0)[0]
        if count > (len(data) - 4) // cls.RECORD_SIZE:
            return None
        entries = []
        for index in range(count):
            base = 4 + index * cls.RECORD_SIZE
            flags = struct.unpack_from("<I", data, base)[0]
            attributes = struct.unpack_from("<I", data, base + 36)[0] if flags & cls._FD_ATTRIBUTES else 0
            high, low = struct.unpack_from("<II", data, base + 64)
            name = data[base + 72:base + cls.RECORD_SIZE]
            end = next((offset for offset in range(0, len(name) - 1, 2) if name[offset] == 0 and name[offset + 1] == 0),
                       len(name) & ~1)
            path = safe_relative_path(name[:end].decode("utf-16-le", "replace"))
            if path is None:
                return None
            modified = None
            if flags & cls._FD_WRITESTIME:
                ticks = struct.unpack_from("<Q", data, base + 56)[0]
                if ticks > 0:
                    modified = (ticks - cls._EPOCH_TICKS) / 10_000_000
            entries.append(ClipboardFileEntry(path=path, is_directory=bool(attributes & cls._DIRECTORY),
                                              size=(high << 32 | low) if flags & cls._FD_FILESIZE else 0,
                                              modified=modified))
        return entries


def safe_relative_path(path: str) -> str | None:
    """"a\\b" from "a\\b", "a/b" or "\\a\\b\\"; None for "..", empty, drive or device paths."""
    parts = [part for part in path.replace("/", "\\").split("\\") if part and part != "."]
    if not parts or any(part == ".." or ":" in part or "\0" in part for part in parts):
        return None
    return "\\".join(parts)


# MARK: Local files (served to the remote)

def parse_file_uris(data: bytes, mime: str) -> list[str]:
    """Local paths from text/uri-list (RFC 2483) or x-special/gnome-copied-files ("copy" or "cut",
    then one URI per line). Non-file URIs are skipped."""
    lines = data.decode("utf-8", "replace").replace("\r\n", "\n").split("\n")
    if mime.startswith("x-special/") and lines and lines[0].strip() in ("copy", "cut"):
        lines = lines[1:]
    result = []
    for line in lines:
        line = line.strip().rstrip("\0")
        if not line or line.startswith("#"):
            continue
        parts = urlsplit(line)
        if parts.scheme != "file" or parts.netloc not in ("", "localhost"):
            continue
        result.append(unquote(parts.path))
    return result


def file_uri(path: str | Path) -> str:
    return "file://" + quote(str(path), safe="/-_.~!$&'()*+,;=:@")


class LocalFileList:
    """Files copied locally, as offered to the server: folders are walked recursively (folders come
    before their contents), and file data is read from disk only when the server asks for it.
    Not thread safe: use it from one worker thread."""

    #: Largest piece read for one request; the server asks again for the rest.
    MAX_CHUNK = 4 << 20
    _SKIPPED_NAMES = {".DS_Store"}

    def __init__(self, entries: list[ClipboardFileEntry], files: list[str]):
        self.entries = entries
        self.total_size = sum(entry.size for entry in entries)
        self._files = files
        self._open: tuple[int, object] | None = None

    @classmethod
    def create(cls, local_paths: list[str], max_total_size: int) -> LocalFileList | None:
        """None if there is nothing to offer or the files are larger than `max_total_size` together
        (`total_of(...)` tells the size for a message)."""
        entries, files = cls._walk(local_paths)
        total = sum(entry.size for entry in entries)
        if not entries:
            return None
        if total > max_total_size:
            log.info("not offering %d bytes of files: over the limit of %d", total, max_total_size)
            return None
        return cls(entries, files)

    @classmethod
    def total_of(cls, local_paths: list[str]) -> int:
        return sum(entry.size for entry in cls._walk(local_paths)[0])

    @classmethod
    def _walk(cls, local_paths: list[str]) -> tuple[list[ClipboardFileEntry], list[str]]:
        entries: list[ClipboardFileEntry] = []
        files: list[str] = []

        def add(path: str, relative: str) -> bool:
            try:
                info = os.stat(path)
            except OSError:
                return False
            is_directory = stat.S_ISDIR(info.st_mode)
            if not is_directory and not stat.S_ISREG(info.st_mode):
                return False
            if len(relative.encode("utf-16-le")) // 2 > FileGroupDescriptor.MAX_PATH_LENGTH:
                log.info("skipping a path too long for Windows")
                return False
            entries.append(ClipboardFileEntry(relative, is_directory, 0 if is_directory else info.st_size,
                                              info.st_mtime))
            files.append(path)
            return is_directory

        def walk(directory: str, relative: str) -> None:
            try:
                names = sorted(os.listdir(directory))
            except OSError:
                return
            for name in names:
                if name in cls._SKIPPED_NAMES:
                    continue
                child = os.path.join(directory, name)
                child_relative = relative + "\\" + name
                # Symlinked folders are listed but not entered (no loops, nothing outside the copy).
                if add(child, child_relative) and not os.path.islink(child):
                    walk(child, child_relative)

        for local in local_paths:
            resolved = os.path.realpath(local)
            name = os.path.basename(resolved.rstrip("/")) or resolved
            if add(resolved, name):
                walk(resolved, name)
        return entries, files

    @property
    def descriptor(self) -> bytes:
        return FileGroupDescriptor.encode(self.entries)

    def answer(self, request: RemoteFileRequest) -> bytes | None:
        """The answer to a server request, None if it cannot be served."""
        if not 0 <= request.file_index < len(self.entries):
            return None
        entry = self.entries[request.file_index]
        if request.size_only:
            return struct.pack("<Q", entry.size)
        if entry.is_directory:
            return None
        try:
            handle = self._handle(request.file_index)
            handle.seek(request.offset)
            return handle.read(min(request.length, self.MAX_CHUNK))
        except OSError as error:
            log.error("reading a copied file failed: %s", error)
            return None

    def close(self) -> None:
        if self._open is not None:
            self._open[1].close()
            self._open = None

    def _handle(self, index: int):
        if self._open is not None and self._open[0] == index:
            return self._open[1]
        self.close()
        handle = open(self._files[index], "rb")  # noqa: SIM115 - kept open across requests
        self._open = (index, handle)
        return handle


# MARK: Remote files (fetched from the remote)

class RemoteFileFetcher:
    """Fetches file contents from the server's clipboard (FileContents requests). Answers carry the
    stream id of their request, so several chunks are in flight at once. Thread safe; call the
    blocking methods from worker threads, `deliver` from the main loop."""

    #: Bytes per request and requests in flight per file.
    CHUNK_SIZE = 1 << 20
    WINDOW = 4
    #: A download fails when no answer arrived for this long.
    STALL_TIMEOUT = 30.0

    def __init__(self, send: Callable[[RemoteFileRequest], bool]):
        self._condition = threading.Condition()
        self._send = send
        # Guarded by _condition.
        self._generation = 0
        self._next_stream_id = 1
        self._answers: dict[int, bytes | None] = {}
        self._waiting: set[int] = set()

    @property
    def generation(self) -> int:
        """Identifies the current remote file list."""
        with self._condition:
            return self._generation

    def reset(self) -> None:
        """The remote clipboard changed or the channel closed: every request in flight fails."""
        with self._condition:
            self._generation += 1
            self._answers = {}
            self._waiting = set()
            self._condition.notify_all()

    def deliver(self, stream_id: int, data: bytes | None) -> None:
        """An answer from the server."""
        with self._condition:
            if stream_id in self._waiting:
                self._waiting.discard(stream_id)
                self._answers[stream_id] = data
                self._condition.notify_all()

    def size(self, file_index: int, generation: int) -> int | None:
        """The size of file `file_index`, asked from the server."""
        stream_id = self._start(RemoteFileRequest(0, file_index, True, 0, 8), generation)
        if stream_id is None:
            return None
        answers = self._wait({stream_id}, generation)
        data = answers.get(stream_id) if answers else None
        if data is None or len(data) < 8:
            return None
        return struct.unpack_from("<Q", data, 0)[0]

    def download(self, file_index: int, size: int, generation: int, write: Callable[[bytes], None],
                 progress: Callable[[int], None] = lambda _bytes: None) -> bool:
        """Streams file `file_index` (`size` bytes) to `write`, in order. False on failure, a stall, a
        clipboard change, or when `write` raises."""
        chunk = self.CHUNK_SIZE  # shrinks to the server's cap: the largest answer it gave
        largest_answer = 0
        next_offset = 0
        gaps: list[tuple[int, int]] = []  # left by short answers, requested first
        in_flight: dict[int, tuple[int, int]] = {}
        received: dict[int, bytes] = {}
        written = 0
        while written < size:
            while len(in_flight) < self.WINDOW:
                if gaps:
                    offset, length = gaps.pop(0)
                elif next_offset < size:
                    offset, length = next_offset, min(chunk, size - next_offset)
                    next_offset += length
                else:
                    break
                stream_id = self._start(RemoteFileRequest(0, file_index, False, offset, length), generation)
                if stream_id is None:
                    return False
                in_flight[stream_id] = (offset, length)
            answers = self._wait(set(in_flight), generation)
            if answers is None:
                return False
            for stream_id, data in answers.items():
                offset, length = in_flight.pop(stream_id)
                if not data:
                    return False
                piece = data[:length]
                received[offset] = piece
                largest_answer = max(largest_answer, len(piece))
                if len(piece) < length:
                    gaps.append((offset + len(piece), length - len(piece)))
                    if largest_answer < chunk:
                        log.info("server answers at most %d bytes per file request", largest_answer)
                        chunk = largest_answer
            while written in received:
                piece = received.pop(written)
                try:
                    write(piece)
                except Exception:  # noqa: BLE001 - a failed write fails the download
                    log.exception("writing a downloaded file failed")
                    return False
                written += len(piece)
                progress(written)
        return True

    def _start(self, request: RemoteFileRequest, generation: int) -> int | None:
        """Registers a request and sends it; its stream id, or None."""
        with self._condition:
            if generation != self._generation:
                return None
            stream_id = self._next_stream_id
            self._next_stream_id = 1 if self._next_stream_id >= 0xFFFFFFFF else self._next_stream_id + 1
            self._waiting.add(stream_id)
        sent = self._send(RemoteFileRequest(stream_id, request.file_index, request.size_only, request.offset,
                                            request.length))
        if not sent:
            with self._condition:
                self._waiting.discard(stream_id)
            return None
        return stream_id

    def _wait(self, ids: set[int], generation: int) -> dict[int, bytes | None] | None:
        """Waits until at least one of `ids` is answered; returns and removes all answered ones.
        None on a stall, a generation change or a reset."""
        deadline = time.monotonic() + self.STALL_TIMEOUT
        with self._condition:
            while True:
                if generation != self._generation:
                    return None
                done = [stream_id for stream_id in ids if stream_id in self._answers]
                if done:
                    return {stream_id: self._answers.pop(stream_id) for stream_id in done}
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._condition.wait(remaining):
                    if not any(stream_id in self._answers for stream_id in ids):
                        self._waiting -= ids
                        return None


class RemoteFileTransfer:
    """Files copied on the server, offered locally as a lazy file list. `materialize_all` downloads
    every copied file and folder into a private folder of the clipboard cache and returns their
    local paths; the reading app then copies them."""

    #: Downloaded files stay this long after the clipboard moved on (an app may still be copying them).
    KEEP_AFTER_USE = 10 * 60
    #: Downloads of earlier runs older than this are removed at start.
    STALE_AFTER = 24 * 60 * 60

    def __init__(self, entries: list[ClipboardFileEntry], fetcher: RemoteFileFetcher, generation: int,
                 root: Path | None = None):
        self.entries = entries
        self._fetcher = fetcher
        self._generation = generation
        self.directory = (root or paths.clipboard_cache_dir()) / uuid.uuid4().hex
        self._lock = threading.Lock()  # serializes downloads
        self._materialized: dict[int, str] = {}

    @property
    def total_size(self) -> int:
        return sum(entry.size for entry in self.entries)

    @property
    def top_level_indices(self) -> list[int]:
        return [index for index, entry in enumerate(self.entries) if entry.is_top_level]

    def materialize_all(self, progress: Callable[[int, int], None] = lambda _done, _total: None) -> list[str] | None:
        """Downloads every top-level entry with everything below it; their local paths, or None."""
        result = []
        done = 0
        for index in self.top_level_indices:
            local = self.materialize(index, lambda bytes_done, base=done: progress(base + bytes_done, self.total_size))
            if local is None:
                return None
            done += sum(entry.size for i, entry in enumerate(self.entries) if self._belongs(i, index))
            result.append(local)
        return result

    def materialize(self, index: int, progress: Callable[[int], None] = lambda _bytes: None) -> str | None:
        """Downloads the top-level entry `index` with everything below it; its local path, or None."""
        with self._lock:
            if index in self._materialized:
                return self._materialized[index]
            top = self.entries[index]
            started = time.monotonic()
            done = 0
            for entry_index, entry in enumerate(self.entries):
                if not self._belongs(entry_index, index):
                    continue
                target = self.directory.joinpath(*entry.components)
                if not self._download(entry_index, target, lambda written, base=done: progress(base + written)):
                    log.error("download of a copied file failed")
                    failed = self.directory.joinpath(*top.components)
                    if failed.is_dir():
                        shutil.rmtree(failed, ignore_errors=True)
                    else:
                        failed.unlink(missing_ok=True)
                    return None
                done += entry.size
            log.info("downloaded %d bytes in %.2f s", done, time.monotonic() - started)
            local = str(self.directory.joinpath(*top.components))
            self._materialized[index] = local
            return local

    def _belongs(self, entry_index: int, top_index: int) -> bool:
        top = self.entries[top_index]
        return entry_index == top_index or self.entries[entry_index].path.startswith(top.path + "\\")

    def _download(self, index: int, target: Path, progress: Callable[[int], None]) -> bool:
        entry = self.entries[index]
        try:
            if entry.is_directory:
                target.mkdir(parents=True, exist_ok=True)
                return True
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as handle:
                size = entry.size if entry.size > 0 else (self._fetcher.size(index, self._generation) or 0)
                if not self._fetcher.download(index, size, self._generation, handle.write, progress):
                    return False
            if entry.modified is not None:
                os.utime(target, (entry.modified, entry.modified))
            return True
        except OSError as error:
            log.error("writing a downloaded file failed: %s", error)
            return False

    def discard(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)

    @classmethod
    def purge_stale(cls, root: Path | None = None, older_than: float | None = None) -> None:
        """Removes downloads of earlier runs."""
        root = root or paths.clipboard_cache_dir()
        limit = cls.STALE_AFTER if older_than is None else older_than
        try:
            folders = list(root.iterdir())
        except OSError:
            return
        now = time.time()
        for folder in folders:
            try:
                if now - folder.stat().st_mtime > limit:
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                pass
