"""Keeps the desktop clipboard and one session's clipboard in sync, in both directions.

Local -> Windows: a changed clipboard ("changed" signal while watching, or `check_local`) is announced
as a format list; the server then asks for data, which is read from the clipboard asynchronously,
converted on a worker thread and sent. Windows -> local: a server format list becomes lazy clipboard
content (GDK content serializers on a per-format-set GType); data is fetched from the server only
when an app reads it. Text is prefetched so it survives the session. Files: weitblick.clipboard.files.

After a reconnect the channel comes up again and the sync announces the clipboard afresh. Agent
sessions pass no clipboard (`clipboard=None`): nothing touches the desktop clipboard, and only text
set with `set_text` is offered to the server.

Port of WeitblickKit's ClipboardSync, ClipboardFiles and RemotePasteboardPromise.
"""

from __future__ import annotations

import itertools
import logging
import os
import queue
import threading
import time
from collections import deque
from typing import Callable

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, GObject  # noqa: E402

from ..i18n import _, ngettext  # noqa: E402
from ..rdp.session import RemoteClipboard, RemoteClipboardFormat, RemoteFileRequest  # noqa: E402
from . import text as cf_text  # noqa: E402
from .fetcher import RemoteDataFetcher  # noqa: E402
from .files import (DEFAULT_MAX_TRANSFER_SIZE, FileGroupDescriptor, LocalFileList, RemoteFileFetcher,  # noqa: E402
                    RemoteFileTransfer, file_uri, parse_file_uris)
from .formats import (CF_UNICODETEXT, FILE_GROUP_DESCRIPTOR, FILE_MIMES, FIRST_REGISTERED_ID, FLAVORS,  # noqa: E402
                      UNICODE_TEXT, Flavor, Offer, TextFlavor, WindowsFormat, offers_for)

log = logging.getLogger(__name__)

#: Remote files are offered as text/uri-list only. Nautilus and the desktop-icons extension read
#: x-special/gnome-copied-files on every clipboard change (to grey out cut files), which would
#: download every file copied in Windows at once; text/uri-list is read only on paste, and GTK 4
#: file managers paste it (GdkFileList).
REMOTE_FILE_MIMES = ("text/uri-list",)
_TEXT_MIME = "text/plain;charset=utf-8"
_READ_CHUNK = 1 << 20
_MAX_URI_LIST = 16 << 20


# MARK: - Threads and the main loop

def _on_main(function: Callable, *args) -> None:
    def run() -> bool:
        try:
            function(*args)
        except Exception:  # noqa: BLE001 - one failed callback must not kill the main loop
            log.exception("clipboard callback failed")
        return GLib.SOURCE_REMOVE
    GLib.idle_add(run)


def _spawn(function: Callable, *args) -> None:
    def run() -> None:
        try:
            function(*args)
        except Exception:  # noqa: BLE001
            log.exception("clipboard worker failed")
    threading.Thread(target=run, name="weitblick-clipboard", daemon=True).start()


class _SerialWorker:
    """Runs submitted functions one after another on a background thread (answers to the server
    leave in request order)."""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._stopped = False

    def submit(self, function: Callable[[], None]) -> None:
        if self._stopped:
            return
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="weitblick-clipboard-responder", daemon=True)
            self._thread.start()
        self._queue.put(function)

    def stop(self) -> None:
        self._stopped = True
        if self._thread is not None:
            self._queue.put(None)

    def _run(self) -> None:
        while (function := self._queue.get()) is not None:
            try:
                function()
            except Exception:  # noqa: BLE001
                log.exception("clipboard responder failed")


# MARK: - Lazy remote content (GDK serializers)

class RemoteContent(GObject.Object):
    """Remote clipboard content on the desktop clipboard. Subclassed once per set of offered MIME
    types (`content_type_for`), because GDK offers every MIME type registered for a GType. The state
    lives in `_OFFERS` under `token`, a GObject property, so it does not depend on the Python wrapper."""

    __gtype_name__ = "WBRemoteClipboardContent"
    token = GObject.Property(type=int, default=0)


_CONTENT_TYPES: dict[tuple[str, ...], type[RemoteContent]] = {}
_OFFERS: dict[int, _RemoteOffer] = {}
_TOKENS = itertools.count(1)


def content_type_for(mimes: tuple[str, ...]) -> type[RemoteContent]:
    """The RemoteContent subclass offering exactly `mimes` (created and registered once)."""
    cls = _CONTENT_TYPES.get(mimes)
    if cls is None:
        name = f"WBRemoteClipboardContent{len(_CONTENT_TYPES) + 1}"
        cls = type(name, (RemoteContent,), {"__gtype_name__": name})
        for mime in mimes:
            Gdk.content_register_serializer(cls.__gtype__, mime, _serialize)
        _CONTENT_TYPES[mimes] = cls
    return cls


def _serialize(serializer: Gdk.ContentSerializer) -> None:
    content = serializer.get_value()
    offer = _OFFERS.get(content.props.token) if isinstance(content, RemoteContent) else None
    if offer is None:
        _fail(serializer, "The remote clipboard content is gone.")
        return
    offer.serve(serializer)


def _write(serializer: Gdk.ContentSerializer, data: bytes) -> None:
    def done(stream: Gio.OutputStream, result: Gio.AsyncResult) -> None:
        try:
            stream.write_all_finish(result)
        except GLib.Error as error:
            serializer.return_error(error)
            return
        serializer.return_success()
    serializer.get_output_stream().write_all_async(data, serializer.get_priority(), serializer.get_cancellable(), done)


def _fail(serializer: Gdk.ContentSerializer, message: str) -> None:
    serializer.return_error(GLib.Error.new_literal(Gio.io_error_quark(), message, Gio.IOErrorEnum.FAILED))


class _RemoteOffer:
    """One server clipboard as published locally. Data is fetched when an app reads a MIME type
    (concurrent reads of one type share the fetch) and kept for later reads."""

    def __init__(self, fetcher: RemoteDataFetcher, timeout: float, offers: dict[str, Offer] | None = None,
                 transfer: RemoteFileTransfer | None = None, status: Callable[[str], None] | None = None):
        self.token = next(_TOKENS)
        self.generation = fetcher.generation
        self.transfer = transfer
        self._fetcher = fetcher
        self._timeout = timeout
        self._status = status
        self._by_mime: dict[str, Offer] = {}
        for offer in (offers or {}).values():
            for mime in offer.flavor.offered_mimes:
                self._by_mime.setdefault(mime, offer)
        self.mimes: tuple[str, ...] = REMOTE_FILE_MIMES if transfer is not None else tuple(self._by_mime)
        self._cache: dict[str, bytes] = {}
        self._waiting: dict[str, list[Gdk.ContentSerializer]] = {}

    def has(self, flavor_name: str) -> bool:
        return any(offer.flavor.name == flavor_name for offer in self._by_mime.values())

    def serve(self, serializer: Gdk.ContentSerializer) -> None:
        mime = serializer.get_mime_type()
        if mime in self._cache:
            _write(serializer, self._cache[mime])
        elif mime in self._waiting:
            self._waiting[mime].append(serializer)
        elif self.transfer is None and mime not in self._by_mime:
            _fail(serializer, f"Cannot provide {mime}.")
        else:
            self._waiting[mime] = [serializer]
            _spawn(self._produce, mime)

    def cached_text(self) -> bytes | None:
        """The text if it has already been fetched (no server round trip)."""
        if _TEXT_MIME in self._cache:
            return self._cache[_TEXT_MIME]
        offer = self._by_mime.get(_TEXT_MIME)
        raw = self._fetcher.cached_data(offer.format_id) if offer else None
        return offer.flavor.to_local(raw, offer.fmt, _TEXT_MIME) if raw is not None else None

    def prefetch_text(self) -> None:
        """Fetches the text in the background so it can still be pasted after the session ends."""
        offer = self._by_mime.get(_TEXT_MIME)
        if offer is None:
            return

        def work() -> None:
            raw = self._fetcher.fetch(offer.format_id, 30)
            if raw is not None:
                data = offer.flavor.to_local(raw, offer.fmt, _TEXT_MIME)
                if data is not None:
                    _on_main(self._cache.setdefault, _TEXT_MIME, data)
        _spawn(work)

    def release(self) -> None:
        """The desktop clipboard moved on: free the fetched data, remove downloads after a while."""
        _OFFERS.pop(self.token, None)
        self._fetcher.discard(self.generation)
        transfer = self.transfer
        if transfer is not None:
            GLib.timeout_add_seconds(RemoteFileTransfer.KEEP_AFTER_USE,
                                     lambda: (_spawn(transfer.discard), GLib.SOURCE_REMOVE)[1])

    # Worker side

    def _produce(self, mime: str) -> None:
        data = self._materialize() if self.transfer is not None else self._convert(mime)
        _on_main(self._finish, mime, data)

    def _convert(self, mime: str) -> bytes | None:
        offer = self._by_mime[mime]
        raw = self._fetcher.fetch(offer.format_id, self._timeout)
        if raw is None:
            return None
        return offer.flavor.to_local(raw, offer.fmt, mime)

    def _materialize(self) -> bytes | None:
        transfer = self.transfer
        assert transfer is not None
        files = sum(1 for entry in transfer.entries if not entry.is_directory)
        self._report(ngettext("Copying %d file from the remote PC…", "Copying %d files from the remote PC…",
                              files) % files)
        last = [time.monotonic()]

        def progress(done: int, total: int) -> None:
            now = time.monotonic()
            if total > 0 and now - last[0] >= 1:
                last[0] = now
                self._report(_("Copying files from the remote PC… %d %%") % (done * 100 // total))

        local = transfer.materialize_all(progress)
        if local is None:
            self._report(_("Copying files from the remote PC failed."))
            return None
        self._report(ngettext("Copied %d file from the remote PC.", "Copied %d files from the remote PC.",
                              files) % files)
        return "".join(file_uri(path) + "\r\n" for path in local).encode("utf-8")

    def _report(self, message: str) -> None:
        if self._status is not None:
            _on_main(self._status, message)

    def _finish(self, mime: str, data: bytes | None) -> None:
        if data is not None:
            self._cache[mime] = data
        for serializer in self._waiting.pop(mime, []):
            if data is None:
                _fail(serializer, "The remote PC did not provide the clipboard content.")
            else:
                _write(serializer, data)


# MARK: - Files

class _FileClipboard:
    """Files in both directions for ClipboardSync (see weitblick.clipboard.files)."""

    def __init__(self, remote: RemoteClipboard, max_transfer_size: int):
        self.fetcher = RemoteFileFetcher(remote.request_file)
        self.max_transfer_size = max_transfer_size
        #: Our id for the descriptor in the current announcement.
        self.announced_format_id: int | None = None
        self._announced_paths: list[str] = []
        self._remote = remote
        self._served: LocalFileList | None = None  # touched on the responder thread only

    def announce(self, local_paths: list[str], format_id: int) -> None:
        self.announced_format_id = format_id
        self._announced_paths = list(local_paths)

    def clear_announcement(self) -> None:
        self.announced_format_id = None
        self._announced_paths = []

    def respond_with_descriptor(self, responder: _SerialWorker, too_large: Callable[[int], None]) -> None:
        """The server asked for the announced descriptor: list the files and answer."""
        local_paths = list(self._announced_paths)
        remote, limit = self._remote, self.max_transfer_size

        def work() -> None:
            if self._served is not None:
                self._served.close()
            self._served = LocalFileList.create(local_paths, limit)
            if self._served is None:
                total = LocalFileList.total_of(local_paths)
                if total > limit:
                    _on_main(too_large, total)
            else:
                log.info("offering %d files and folders, %d bytes", len(self._served.entries),
                         self._served.total_size)
            remote.respond(self._served.descriptor if self._served is not None else None)
        responder.submit(work)

    def answer(self, request: RemoteFileRequest, responder: _SerialWorker) -> None:
        def work() -> None:
            served = self._served
            self._remote.respond_file(request.stream_id, served.answer(request) if served is not None else None)
        responder.submit(work)

    def receive(self, descriptor_format_id: int, data_fetcher: RemoteDataFetcher,
                publish: Callable[[RemoteFileTransfer], None], too_large: Callable[[int], None]) -> None:
        """The server offers files: fetch its descriptor in the background, then publish the transfer
        on the main loop, unless the server clipboard changed meanwhile."""
        fetcher, generation, limit = self.fetcher, self.fetcher.generation, self.max_transfer_size

        def work() -> None:
            data = data_fetcher.fetch(descriptor_format_id, 10)
            entries = FileGroupDescriptor.decode(data) if data is not None else None
            if not entries:
                # A newer server clipboard (Windows often announces twice) cancels the fetch quietly.
                if fetcher.generation == generation:
                    log.error("server file list unreadable")
                return
            transfer = RemoteFileTransfer(entries, fetcher, generation)
            if transfer.total_size > limit:
                log.info("not taking %d bytes of files: over the limit of %d", transfer.total_size, limit)
                _on_main(too_large, transfer.total_size)
                return

            def on_main() -> None:
                if fetcher.generation == generation:
                    log.info("server copied %d files and folders, %d bytes", len(entries), transfer.total_size)
                    publish(transfer)
            _on_main(on_main)
        _spawn(work)

    def reset(self) -> None:
        self.fetcher.reset()

    def close(self, responder: _SerialWorker) -> None:
        """Closes the file the server was reading (on the responder, after its pending answers)."""
        def work() -> None:
            if self._served is not None:
                self._served.close()
                self._served = None
        responder.submit(work)


_purged = False


# MARK: - ClipboardSync

class ClipboardSync:
    """Keeps a Gdk.Clipboard and one session's RemoteClipboard in sync; becomes its delegate.

    Drive it from the session: `start_watching()` + `check_local()` while the session window is
    focused, `stop_watching()` when it loses focus, `enabled` for the session menu toggle, and
    `session_will_end()` before the session is closed. All methods run on the GTK main loop.
    """

    DEFAULT_MAX_DATA_SIZE = 128 << 20
    DEFAULT_MAX_FILE_TRANSFER_SIZE = DEFAULT_MAX_TRANSFER_SIZE
    #: How long an app reading remote content may wait for the server.
    READ_TIMEOUT = 10.0

    def __init__(self, remote: RemoteClipboard, clipboard: Gdk.Clipboard | None,
                 max_data_size: int = DEFAULT_MAX_DATA_SIZE,
                 max_file_transfer_size: int = DEFAULT_MAX_FILE_TRANSFER_SIZE):
        self._remote = remote
        self._clipboard = clipboard
        self._max_data_size = max_data_size
        self._fetcher = RemoteDataFetcher(max_data_size, remote.request)
        self._files = _FileClipboard(remote, max_file_transfer_size)
        self._flavors: tuple[Flavor, ...] = FLAVORS
        self._responder = _SerialWorker()
        self._enabled = True
        self._channel_ready = False
        self._channel_epoch = 0
        self._watching = False
        self._local_changes = 0
        self._last_seen: int | None = None
        self._announce_token = 0
        self._announced: dict[int, tuple[Flavor, WindowsFormat]] = {}
        self._remote_formats: list[RemoteClipboardFormat] = []
        #: Text put on the server clipboard with set_text; the server's clipboard owner is us until
        #: it announces a change or the local clipboard is announced again.
        self._offered_text: str | None = None
        self._text_waiters: list[tuple[int, Callable[[bool], None], int]] = []
        self._requests: deque[int] = deque()
        self._request_busy = False
        self._own_provider: Gdk.ContentProvider | None = None
        self._own_offer: _RemoteOffer | None = None
        self._ended = False
        #: Format lists the server accepted from us.
        self.accepted_announcements = 0
        #: Server clipboard changes published to the desktop clipboard.
        self.published_remote_changes = 0
        #: Format lists the server announced (counted even while disabled); lets agents see a copy land.
        self.remote_change_count = 0
        #: User-visible progress and problems ("Copying 3 files from the remote PC…"), main loop.
        self.on_status: Callable[[str], None] | None = None
        self._changed_handler = clipboard.connect("changed", self._on_local_changed) if clipboard is not None else 0
        remote.delegate = self
        global _purged
        if not _purged:
            _purged = True
            _spawn(RemoteFileTransfer.purge_stale)

    # MARK: Public API

    @property
    def enabled(self) -> bool:
        """Off: nothing is announced, published or answered (the channel stays open)."""
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        if value == self._enabled:
            return
        self._enabled = value
        self._last_seen = None
        if value:
            self.check_local()

    @property
    def is_channel_ready(self) -> bool:
        return self._channel_ready

    def start_watching(self) -> None:
        """Announce local clipboard changes as they happen (the session window has the focus)."""
        self._watching = True

    def stop_watching(self) -> None:
        self._watching = False

    def check_local(self) -> None:
        """Announces the local clipboard to the server if it changed since the last look."""
        self._announce_local(force=False)

    def session_will_end(self) -> None:
        """Call before the session goes away: remote text published on the clipboard is written out
        so it can still be pasted; other remote content keeps only what was already fetched."""
        if self._ended:
            return
        self._ended = True
        self._watching = False
        if self._remote.delegate is self:
            self._remote.delegate = None
        if self._clipboard is not None and self._changed_handler:
            self._clipboard.disconnect(self._changed_handler)
            self._changed_handler = 0
        if self._is_own_content() and self._own_offer is not None:
            text = self._own_offer.cached_text()
            if text is not None:
                self._release_own_content()
                self._clipboard.set_content(
                    Gdk.ContentProvider.new_for_value(GObject.Value(str, text.decode("utf-8", "replace"))))
        self._fetcher.reset()
        self._files.reset()
        self._files.close(self._responder)
        self._responder.stop()
        self._channel_ready = False
        self._resolve_text_waiters(False)

    def get_remote_text(self, callback: Callable[[str | None], None], timeout: float = 10.0) -> None:
        """The server clipboard as text, read straight from the channel (never the desktop
        clipboard): the text set with set_text if it is still the server's clipboard, else the
        server's CF_UNICODETEXT. None when it holds no text, the channel is down or the server does
        not answer within `timeout`. The callback runs on the main loop."""
        if self._offered_text is not None:
            _on_main(callback, self._offered_text)
            return
        fmt = next((f for f in self._remote_formats if UNICODE_TEXT.matches(f)), None)
        if not self._channel_ready or fmt is None:
            _on_main(callback, None)
            return
        fetcher = self._fetcher

        def work() -> None:
            data = fetcher.fetch(fmt.id, timeout)
            _on_main(callback, cf_text.decode(data) if data is not None else None)
        _spawn(work)

    def set_text(self, text: str, callback: Callable[[bool], None] | None = None, timeout: float = 5.0) -> bool:
        """Puts text on the server clipboard (announces CF_UNICODETEXT and answers the server's request
        with it, also while the sync is off). `callback(True)` once the server accepted the format
        list, `callback(False)` on failure or after `timeout`. Returns False if nothing was sent."""
        if not self._channel_ready:
            if callback is not None:
                _on_main(callback, False)
            return False
        before = self.accepted_announcements
        self._announce_token += 1  # a local announcement in progress must not overwrite this
        self._announced = {}
        self._files.clear_announcement()
        self._offered_text = text
        if not self._remote.announce([RemoteClipboardFormat(CF_UNICODETEXT)]):
            self._offered_text = None
            if callback is not None:
                _on_main(callback, False)
            return False
        if callback is not None:
            entry: list = []

            def expire() -> bool:
                if entry and entry[0] in self._text_waiters:
                    self._text_waiters.remove(entry[0])
                    callback(False)
                return GLib.SOURCE_REMOVE
            source = GLib.timeout_add(int(timeout * 1000), expire)
            entry.append((before, callback, source))
            self._text_waiters.append(entry[0])
        return True

    # MARK: Local -> Windows

    def _on_local_changed(self, clipboard: Gdk.Clipboard) -> None:
        self._local_changes += 1
        if self._own_provider is not None and not self._is_own_content():
            self._release_own_content()
        if self._is_own_content():
            self._last_seen = self._local_changes  # our own remote content: never echoed back
            return
        if self._watching:
            self._announce_local(force=False)

    def _is_own_content(self) -> bool:
        return (self._clipboard is not None and self._own_provider is not None
                and self._clipboard.get_content() is self._own_provider)

    def _announce_local(self, force: bool) -> None:
        if not self._channel_ready:
            return
        count = self._local_changes
        if not force and count == self._last_seen:
            return
        self._last_seen = count
        self._announced = {}
        self._files.clear_announcement()
        self._announce_token += 1
        token = self._announce_token
        clipboard = self._clipboard
        if not self._enabled or clipboard is None or self._is_own_content():
            self._send_announcement(set(), None, force, token)
            return
        mimes = set(clipboard.get_formats().get_mime_types() or [])
        file_mime = next((mime for mime in FILE_MIMES if mime in mimes), None)
        if file_mime is None:
            self._send_announcement(mimes, None, force, token)
            return

        def got(data: bytes | None) -> None:
            local = [path for path in parse_file_uris(data, file_mime) if os.path.exists(path)] if data else []
            self._send_announcement(mimes, local, force, token)
        self._read_mime(file_mime, _MAX_URI_LIST, lambda _mime, data: got(data))

    def _send_announcement(self, mimes: set[str], local_paths: list[str] | None, force: bool, token: int) -> None:
        if token != self._announce_token or not self._channel_ready:
            return  # a newer announcement (or set_text) took over
        formats: list[RemoteClipboardFormat] = []
        next_id = FIRST_REGISTERED_ID
        if local_paths:
            formats.append(RemoteClipboardFormat(next_id, FILE_GROUP_DESCRIPTOR.name))
            self._files.announce(local_paths, next_id)
            next_id += 1
        for flavor in self._flavors:
            if not mimes or not flavor.offers(mimes, has_files=bool(local_paths)):
                continue
            for fmt in flavor.windows_formats:
                if fmt.id is not None:
                    entry = RemoteClipboardFormat(fmt.id)
                else:
                    entry = RemoteClipboardFormat(next_id, fmt.name)
                    next_id += 1
                self._announced[entry.id] = (flavor, fmt)
                formats.append(entry)
        # The channel wants an initial list even if empty; later empty lists mean nothing to send.
        if not force and not formats:
            return
        log.info("announcing %d formats: %s", len(formats), [f.name or f.id for f in formats])
        self._offered_text = None
        self._remote.announce(formats)

    def _process_requests(self) -> None:
        while not self._request_busy and self._requests:
            self._answer_request(self._requests.popleft())

    def _answer_request(self, format_id: int) -> None:
        remote = self._remote
        if self._offered_text is not None and format_id == CF_UNICODETEXT:
            data = cf_text.encode(self._offered_text)
            self._responder.submit(lambda: remote.respond(data))
            return
        if self._enabled and format_id == self._files.announced_format_id:
            self._files.respond_with_descriptor(self._responder, self._files_too_large_locally)
            return
        entry = self._announced.get(format_id) if self._enabled else None
        if entry is None or self._clipboard is None:
            log.info("server asked for format %d, nothing to send", format_id)
            self._responder.submit(lambda: remote.respond(None))
            return
        flavor, fmt = entry
        epoch = self._channel_epoch
        self._request_busy = True
        max_size = self._max_data_size

        def got(mime: str | None, data: bytes | None) -> None:
            if epoch != self._channel_epoch:
                return  # the channel restarted; that request is gone
            self._request_busy = False

            def work() -> None:
                converted = flavor.to_windows(data, mime, fmt) if data is not None and mime else None
                if converted is not None and len(converted) > max_size:
                    log.info("clipboard data of %d bytes exceeds the limit", len(converted))
                    converted = None
                remote.respond(converted)
            self._responder.submit(work)
            self._process_requests()
        self._read_local(flavor, got)

    def _read_local(self, flavor: Flavor, callback: Callable[[str | None, bytes | None], None]) -> None:
        """Reads the local clipboard for `flavor`: (mime, data), or (None, None)."""
        clipboard = self._clipboard
        if isinstance(flavor, TextFlavor):
            def text_done(source: Gdk.Clipboard, result: Gio.AsyncResult) -> None:
                try:
                    value = source.read_text_finish(result)
                except GLib.Error as error:
                    log.info("reading local text failed: %s", error.message)
                    value = None
                callback(_TEXT_MIME, value.encode("utf-8") if value is not None else None)
            clipboard.read_text_async(None, text_done)
            return
        formats = clipboard.get_formats()
        mime = next((m for m in flavor.read_mimes if formats.contain_mime_type(m)), None)
        if mime is None:
            callback(None, None)
            return
        self._read_mime(mime, self._max_data_size, callback)

    def _read_mime(self, mime: str, limit: int, callback: Callable[[str | None, bytes | None], None]) -> None:
        def opened(source: Gdk.Clipboard, result: Gio.AsyncResult) -> None:
            try:
                stream, chosen = source.read_finish(result)
            except GLib.Error as error:
                log.info("reading %s from the clipboard failed: %s", mime, error.message)
                callback(None, None)
                return
            read_stream(stream, limit, lambda data: callback(chosen or mime, data))
        self._clipboard.read_async([mime], GLib.PRIORITY_DEFAULT, None, opened)

    def _files_too_large_locally(self, total: int) -> None:
        self._report(_("The copied files (%s) are larger than %s and aren’t offered to the remote PC.")
                     % (GLib.format_size(total), GLib.format_size(self._files.max_transfer_size)))

    def _files_too_large_remotely(self, total: int) -> None:
        self._report(_("The files copied on the remote PC (%s) are larger than %s and can’t be pasted here.")
                     % (GLib.format_size(total), GLib.format_size(self._files.max_transfer_size)))

    def _report(self, message: str) -> None:
        log.info("%s", message)
        if self.on_status is not None:
            self.on_status(message)

    # MARK: Windows -> local

    def _publish(self, formats: list[RemoteClipboardFormat]) -> None:
        self._remote_formats = formats
        self.remote_change_count += 1
        self._offered_text = None
        if not self._enabled or self._clipboard is None:
            return
        log.info("server clipboard: %s", [f.name or f.id for f in formats])
        # Copied files (Explorer) come with names and shell formats, but the files are the content.
        descriptor = next((f for f in formats if FILE_GROUP_DESCRIPTOR.matches(f)), None)
        if descriptor is not None:
            self._files.receive(descriptor.id, self._fetcher, self._publish_files, self._files_too_large_remotely)
            return
        offers = offers_for(formats, self._flavors)
        if not offers:
            return
        offer = _RemoteOffer(self._fetcher, self.READ_TIMEOUT, offers=offers, status=self._report)
        self._write(offer)
        offer.prefetch_text()

    def _publish_files(self, transfer: RemoteFileTransfer) -> None:
        if self._ended or not self._enabled or self._clipboard is None:
            return
        self._write(_RemoteOffer(self._fetcher, self.READ_TIMEOUT, transfer=transfer, status=self._report))

    def _write(self, offer: _RemoteOffer) -> None:
        cls = content_type_for(offer.mimes)
        provider = Gdk.ContentProvider.new_for_value(GObject.Value(cls.__gtype__, cls(token=offer.token)))
        self._release_own_content()
        # Older offers of other sessions are replaced on the clipboard too.
        for stale in list(_OFFERS.values()):
            stale.release()
        _OFFERS[offer.token] = offer
        self._own_provider = provider
        self._own_offer = offer
        self._clipboard.set_content(provider)  # emits "changed" right away: seen as our own
        self._last_seen = self._local_changes
        self.published_remote_changes += 1

    def _release_own_content(self) -> None:
        if self._own_offer is not None:
            self._own_offer.release()
        self._own_offer = None
        self._own_provider = None

    # MARK: Text waiters

    def _resolve_text_waiters(self, accepted: bool) -> None:
        waiters, self._text_waiters = self._text_waiters, []
        for _before, callback, source in waiters:
            GLib.source_remove(source)
            callback(accepted)

    # MARK: RemoteClipboardDelegate (main loop)

    def remote_clipboard_ready(self, clipboard: RemoteClipboard) -> None:
        self._channel_ready = True
        self._channel_epoch += 1
        self._announce_local(force=True)

    def remote_clipboard_announced(self, clipboard: RemoteClipboard, accepted: bool) -> None:
        if not accepted:
            return
        self.accepted_announcements += 1
        ready = [w for w in self._text_waiters if w[0] < self.accepted_announcements]
        for waiter in ready:
            self._text_waiters.remove(waiter)
            GLib.source_remove(waiter[2])
            waiter[1](True)

    def remote_clipboard_formats(self, clipboard: RemoteClipboard, formats: list[RemoteClipboardFormat]) -> None:
        # Before anything else, so a late answer for the old clipboard is never cached for the new one.
        self._fetcher.reset()
        self._files.reset()
        self._publish(formats)

    def remote_clipboard_data_requested(self, clipboard: RemoteClipboard, format_id: int) -> None:
        self._requests.append(format_id)
        self._process_requests()

    def remote_clipboard_data_received(self, clipboard: RemoteClipboard, data: bytes | None) -> None:
        self._fetcher.deliver(data)

    def remote_clipboard_file_requested(self, clipboard: RemoteClipboard, request: RemoteFileRequest) -> None:
        self._files.answer(request, self._responder)

    def remote_clipboard_file_received(self, clipboard: RemoteClipboard, stream_id: int, data: bytes | None) -> None:
        self._files.fetcher.deliver(stream_id, data)

    def remote_clipboard_closed(self, clipboard: RemoteClipboard) -> None:
        self._fetcher.reset()
        self._files.reset()
        self._channel_ready = False
        self._channel_epoch += 1
        self._requests.clear()
        self._request_busy = False
        self._announced = {}
        self._remote_formats = []
        self._offered_text = None
        self._files.clear_announcement()
        self._resolve_text_waiters(False)


def read_stream(stream: Gio.InputStream, limit: int, callback: Callable[[bytes | None], None]) -> None:
    """Reads `stream` to the end asynchronously; None on error or beyond `limit` bytes."""
    chunks: list[bytes] = []
    total = [0]

    def finish(data: bytes | None) -> None:
        try:
            stream.close(None)
        except GLib.Error:
            pass
        callback(data)

    def on_chunk(source: Gio.InputStream, result: Gio.AsyncResult) -> None:
        try:
            data = source.read_bytes_finish(result).get_data() or b""
        except GLib.Error as error:
            log.info("reading the clipboard failed: %s", error.message)
            finish(None)
            return
        if not data:
            finish(b"".join(chunks))
            return
        total[0] += len(data)
        if total[0] > limit:
            log.info("clipboard data exceeds the limit of %d bytes", limit)
            finish(None)
            return
        chunks.append(bytes(data))
        source.read_bytes_async(_READ_CHUNK, GLib.PRIORITY_DEFAULT, None, on_chunk)

    stream.read_bytes_async(_READ_CHUNK, GLib.PRIORITY_DEFAULT, None, on_chunk)
