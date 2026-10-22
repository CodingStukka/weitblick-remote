"""RemoteDataFetcher and ClipboardSync (port of ClipboardSyncTests plus sync behaviour with fakes).

The sync tests never touch the desktop clipboard: a fake Gdk.Clipboard stands in for it, and remote
content is read through GDK's real serializer machinery (Gdk.content_serialize_async).
"""

import os
import shutil
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, GObject  # noqa: E402

from weitblick.clipboard import html as cf_html  # noqa: E402
from weitblick.clipboard import text as cf_text  # noqa: E402
from weitblick.clipboard.fetcher import RemoteDataFetcher  # noqa: E402
from weitblick.clipboard.files import ClipboardFileEntry, FileGroupDescriptor, file_uri  # noqa: E402
from weitblick.clipboard.sync import ClipboardSync, content_type_for  # noqa: E402
from weitblick.rdp.session import RemoteClipboardFormat, RemoteFileRequest  # noqa: E402


def run_until(predicate, timeout=5.0):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        if not context.iteration(False):
            time.sleep(0.002)


def spin(seconds=0.05):
    deadline = time.monotonic() + seconds
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        if not context.iteration(False):
            time.sleep(0.002)


# MARK: RemoteDataFetcher

class FakeServer:
    def __init__(self):
        self.lock = threading.Lock()
        self.sent = []

    @property
    def requests(self):
        with self.lock:
            return list(self.sent)

    def send(self, format_id):
        with self.lock:
            self.sent.append(format_id)
        return True


def later(seconds, function, *args):
    threading.Timer(seconds, function, args).start()


class RemoteDataFetcherTests(unittest.TestCase):
    def test_fetches_once_and_serves_the_cache(self):
        server = FakeServer()
        fetcher = RemoteDataFetcher(100, server.send)
        later(0.05, fetcher.deliver, b"hi")
        self.assertEqual(fetcher.fetch(13, 2), b"hi")
        self.assertEqual(fetcher.fetch(13, 2), b"hi")
        self.assertEqual(server.requests, [13])

    def test_only_one_request_is_in_flight(self):
        server = FakeServer()
        fetcher = RemoteDataFetcher(100, server.send)
        results = {}
        threading.Thread(target=lambda: fetcher.fetch(1, 2), daemon=True).start()
        time.sleep(0.05)
        second = threading.Thread(target=lambda: results.setdefault(2, fetcher.fetch(2, 2)), daemon=True)
        second.start()
        time.sleep(0.1)
        self.assertEqual(server.requests, [1], "the second request waits for the first answer")
        fetcher.deliver(b"\x01")
        time.sleep(0.1)
        self.assertEqual(server.requests, [1, 2])
        fetcher.deliver(b"\x02")
        second.join(2)
        self.assertEqual(results[2], b"\x02")

    def test_a_late_answer_is_not_taken_for_the_next_request(self):
        server = FakeServer()
        fetcher = RemoteDataFetcher(100, server.send)
        self.assertIsNone(fetcher.fetch(1, 0.1))  # times out, request stays in flight

        def answers():
            fetcher.deliver(b"\x01")  # late answer to request 1
            time.sleep(0.05)
            fetcher.deliver(b"\x02")
        later(0.1, answers)
        self.assertEqual(fetcher.fetch(2, 2), b"\x02")
        self.assertEqual(server.requests, [1, 2])

    def test_answers_for_an_old_clipboard_and_oversized_data_are_dropped(self):
        server = FakeServer()
        fetcher = RemoteDataFetcher(3, server.send)

        def change_then_answer():
            fetcher.reset()  # the server's clipboard changed while we waited
            fetcher.deliver(b"\x01")
        later(0.05, change_then_answer)
        self.assertIsNone(fetcher.fetch(1, 2))
        self.assertIsNone(fetcher.cached_data(1))
        later(0.05, fetcher.deliver, b"\x01\x02\x03\x04")
        self.assertIsNone(fetcher.fetch(1, 2))
        self.assertIsNone(fetcher.fetch(1, 2), "too large stays cached, no second request")
        self.assertEqual(server.requests, [1, 1])

    def test_a_refused_format_is_asked_for_again(self):
        # Windows answers with a failure while its clipboard is still busy right after a change; that
        # answer must not stick to the clipboard (a paste a moment later has to ask again).
        server = FakeServer()
        fetcher = RemoteDataFetcher(100, server.send)
        later(0.05, fetcher.deliver, None)
        self.assertIsNone(fetcher.fetch(13, 2))
        later(0.05, fetcher.deliver, b"hi")
        self.assertEqual(fetcher.fetch(13, 2), b"hi")
        self.assertEqual(server.requests, [13, 13])


# MARK: ClipboardSync

class FakeRemote:
    """Records what the sync sends; answers data requests from `answers` (format id -> bytes)."""

    def __init__(self):
        self.delegate = None
        self.announced = []
        self.requests = []
        self.responses = []
        self.file_requests = []
        self.file_responses = {}
        self.answers = {}
        self.files = {}
        self.lock = threading.Lock()

    def announce(self, formats):
        self.announced.append(list(formats))
        return True

    def request(self, format_id):
        with self.lock:
            self.requests.append(format_id)
        if format_id in self.answers:
            data = self.answers[format_id]
            GLib.idle_add(lambda: (self.delegate.remote_clipboard_data_received(self, data), False)[1])
        return True

    def respond(self, data):
        with self.lock:
            self.responses.append(data)
        return True

    def request_file(self, request):
        with self.lock:
            self.file_requests.append(request)
        content = self.files.get(request.file_index)
        if content is not None:
            answer = (struct.pack("<Q", len(content)) if request.size_only
                      else content[request.offset:request.offset + request.length])
            GLib.idle_add(lambda: (self.delegate.remote_clipboard_file_received(self, request.stream_id, answer), False)[1])
        return True

    def respond_file(self, stream_id, data):
        with self.lock:
            self.file_responses[stream_id] = data
        return True


class FakeClipboard:
    """The parts of Gdk.Clipboard the sync uses, without the desktop."""

    def __init__(self):
        self.handlers = {}
        self.next_handler = 1
        self.content = None
        self.contents_set = []
        self.data = {}  # mime -> bytes, another app's copy
        self.text = None

    def connect(self, signal, handler):
        handler_id = self.next_handler
        self.next_handler += 1
        self.handlers[handler_id] = handler
        return handler_id

    def disconnect(self, handler_id):
        self.handlers.pop(handler_id, None)

    def emit_changed(self):
        for handler in list(self.handlers.values()):
            handler(self)

    def copy(self, data, text=None):
        """Another app copies."""
        self.content = None
        self.data = dict(data)
        self.text = text
        self.emit_changed()

    def set_content(self, provider):
        self.content = provider
        self.contents_set.append(provider)
        self.data = {}
        self.emit_changed()
        return True

    def get_content(self):
        return self.content

    def get_formats(self):
        mimes = list(self.data) + (["text/plain;charset=utf-8"] if self.text is not None else [])
        return Gdk.ContentFormats.new(mimes)

    def read_text_async(self, cancellable, callback):
        GLib.idle_add(lambda: (callback(self, ("text", self.text)), False)[1])

    def read_text_finish(self, result):
        return result[1]

    def read_async(self, mimes, priority, cancellable, callback):
        mime = next(m for m in mimes if m in self.data)
        GLib.idle_add(lambda: (callback(self, ("data", mime)), False)[1])

    def read_finish(self, result):
        mime = result[1]
        return Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(self.data[mime])), mime


def read_remote(sync, mime, timeout=5.0):
    """Reads published remote content like a pasting app (GDK serializes our content type)."""
    offer = sync._own_offer
    cls = content_type_for(offer.mimes)
    value = GObject.Value(cls.__gtype__, cls(token=offer.token))
    out = Gio.MemoryOutputStream.new_resizable()
    result = {}

    def done(source, res):
        try:
            Gdk.content_serialize_finish(res)
            out.close(None)
            result["data"] = out.steal_as_bytes().get_data()
        except GLib.Error as error:
            result["error"] = error.message
    Gdk.content_serialize_async(out, mime, value, GLib.PRIORITY_DEFAULT, None, done)
    run_until(lambda: result, timeout)
    if "error" in result:
        raise AssertionError(result["error"])
    return result["data"]


class ClipboardSyncTests(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix="weitblick-cache-")
        self.old_cache = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = self.cache
        self.remote = FakeRemote()
        self.clipboard = FakeClipboard()
        self.syncs = []

    def tearDown(self):
        for sync in self.syncs:
            sync.session_will_end()
        spin(0.02)
        if self.old_cache is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self.old_cache
        shutil.rmtree(self.cache, ignore_errors=True)

    def make(self, clipboard="fake"):
        sync = ClipboardSync(self.remote, self.clipboard if clipboard == "fake" else clipboard)
        self.syncs.append(sync)
        return sync

    def test_isolated_sync_announces_an_empty_list_and_offers_text_only_on_request(self):
        sync = self.make(clipboard=None)
        self.assertIs(self.remote.delegate, sync)
        sync.remote_clipboard_ready(self.remote)
        self.assertEqual(self.remote.announced, [[]])
        accepted = []
        self.assertTrue(sync.set_text("a\nb", accepted.append))
        self.assertEqual(self.remote.announced[-1], [RemoteClipboardFormat(13)])
        sync.remote_clipboard_announced(self.remote, True)
        self.assertEqual(accepted, [True])
        sync.remote_clipboard_data_requested(self.remote, 13)
        run_until(lambda: self.remote.responses)
        self.assertEqual(self.remote.responses, [cf_text.encode("a\nb")])
        got = []
        sync.get_remote_text(got.append)
        run_until(lambda: got)
        self.assertEqual(got, ["a\nb"])

    def test_set_text_fails_without_a_channel_and_times_out_without_acceptance(self):
        sync = self.make(clipboard=None)
        results = []
        self.assertFalse(sync.set_text("x", results.append))
        run_until(lambda: results)
        self.assertEqual(results, [False])
        sync.remote_clipboard_ready(self.remote)
        sync.set_text("x", results.append, timeout=0.05)
        run_until(lambda: len(results) == 2)
        self.assertEqual(results, [False, False])

    def test_remote_text_is_read_from_the_channel(self):
        sync = self.make(clipboard=None)
        sync.remote_clipboard_ready(self.remote)
        self.remote.answers[13] = cf_text.encode("Grüße\nWelt")
        sync.remote_clipboard_formats(self.remote, [RemoteClipboardFormat(13), RemoteClipboardFormat(0xC0A0, "HTML Format")])
        self.assertEqual(sync.remote_change_count, 1)
        got = []
        sync.get_remote_text(got.append)
        run_until(lambda: got)
        self.assertEqual(got, ["Grüße\nWelt"])
        self.assertEqual(self.clipboard.contents_set, [], "an isolated sync never publishes")

    def test_local_text_and_html_are_announced_and_answered(self):
        sync = self.make()
        sync.start_watching()
        self.clipboard.data = {"text/html": b"<b>Hi</b>"}
        self.clipboard.text = "eins\nzwei"
        sync.remote_clipboard_ready(self.remote)
        self.assertEqual(self.remote.announced, [[RemoteClipboardFormat(13), RemoteClipboardFormat(0xC000, "HTML Format")]])
        sync.remote_clipboard_data_requested(self.remote, 0xC000)
        sync.remote_clipboard_data_requested(self.remote, 13)
        sync.remote_clipboard_data_requested(self.remote, 99)
        run_until(lambda: len(self.remote.responses) == 3)
        self.assertEqual(self.remote.responses, [cf_html.encode("<b>Hi</b>"), cf_text.encode("eins\nzwei"), None])

        # A later copy is announced while watching, not while not watching (until check_local).
        sync.stop_watching()
        self.clipboard.copy({}, text="drei")
        self.assertEqual(len(self.remote.announced), 1)
        sync.check_local()
        self.assertEqual(self.remote.announced[-1], [RemoteClipboardFormat(13)])
        sync.check_local()
        self.assertEqual(len(self.remote.announced), 2, "unchanged: no second announcement")

    def test_local_images_are_offered_in_three_formats(self):
        from weitblick.clipboard import dib, image
        sync = self.make()
        source = dib.BGRAImage(2, 1, bytes([1, 2, 3, 255, 4, 5, 6, 128]))
        self.clipboard.data = {"image/png": image.encode_png(source)}
        sync.remote_clipboard_ready(self.remote)
        self.assertEqual(self.remote.announced[0], [RemoteClipboardFormat(0xC000, "PNG"), RemoteClipboardFormat(17),
                                                    RemoteClipboardFormat(8)])
        sync.remote_clipboard_data_requested(self.remote, 17)
        run_until(lambda: self.remote.responses)
        self.assertEqual(dib.decode_dib(self.remote.responses[0]), source)

    def test_disabled_sync_announces_and_publishes_nothing(self):
        sync = self.make()
        sync.start_watching()
        sync.enabled = False
        self.clipboard.text = "x"
        sync.remote_clipboard_ready(self.remote)
        self.assertEqual(self.remote.announced, [[]], "only the initial empty list")
        sync.remote_clipboard_formats(self.remote, [RemoteClipboardFormat(13)])
        self.assertEqual(self.clipboard.contents_set, [])
        self.assertEqual(sync.remote_change_count, 1)
        sync.remote_clipboard_data_requested(self.remote, 13)
        run_until(lambda: self.remote.responses)
        self.assertEqual(self.remote.responses, [None])
        sync.enabled = True
        self.assertEqual(self.remote.announced[-1], [RemoteClipboardFormat(13)])

    def test_remote_content_is_published_lazily_and_never_echoed(self):
        sync = self.make()
        sync.start_watching()
        sync.remote_clipboard_ready(self.remote)
        self.remote.answers = {13: cf_text.encode("a\r\nb"), 0xC0A1: cf_html.encode("<i>x</i>")}
        sync.remote_clipboard_formats(self.remote, [RemoteClipboardFormat(13), RemoteClipboardFormat(0xC0A1, "HTML Format"),
                                                    RemoteClipboardFormat(8)])
        self.assertEqual(len(self.clipboard.contents_set), 1)
        self.assertEqual(self.remote.announced, [[]], "our own content is not announced back")
        offered = Gdk.ContentFormats.new_for_gtype(content_type_for(sync._own_offer.mimes).__gtype__)
        self.assertEqual(set(offered.union_serialize_mime_types().get_mime_types()),
                         {"text/plain;charset=utf-8", "text/plain", "text/html"})
        run_until(lambda: 13 in self.remote.requests)  # text is prefetched
        self.assertEqual(read_remote(sync, "text/plain;charset=utf-8"), b"a\nb")
        self.assertNotIn(0xC0A1, self.remote.requests, "HTML only when read")
        self.assertTrue(read_remote(sync, "text/html").endswith(b"<i>x</i><!--EndFragment--></body></html>"))
        read_remote(sync, "text/html")
        self.assertEqual(self.remote.requests.count(0xC0A1), 1)

    def test_text_survives_the_session(self):
        sync = self.make()
        sync.remote_clipboard_ready(self.remote)
        self.remote.answers = {13: cf_text.encode("bleibt")}
        sync.remote_clipboard_formats(self.remote, [RemoteClipboardFormat(13)])
        run_until(lambda: sync._own_offer.cached_text() is not None)
        ours = self.clipboard.content
        sync.session_will_end()
        self.assertEqual(len(self.clipboard.contents_set), 2)
        self.assertIsNot(self.clipboard.content, ours)
        self.assertIsNone(self.remote.delegate)

    def test_another_copy_releases_the_remote_content(self):
        sync = self.make()
        sync.start_watching()
        sync.remote_clipboard_ready(self.remote)
        sync.remote_clipboard_formats(self.remote, [RemoteClipboardFormat(0xC0A1, "Rich Text Format")])
        self.assertIsNotNone(sync._own_offer)
        self.clipboard.copy({"text/rtf": b"{\\rtf1 y}"})
        self.assertIsNone(sync._own_offer)
        self.assertEqual(self.remote.announced[-1], [RemoteClipboardFormat(0xC000, "Rich Text Format")])

    def test_closing_the_channel_forgets_the_announcement(self):
        sync = self.make()
        self.clipboard.text = "x"
        sync.remote_clipboard_ready(self.remote)
        sync.remote_clipboard_closed(self.remote)
        self.assertFalse(sync.is_channel_ready)
        sync.remote_clipboard_data_requested(self.remote, 13)  # a request arriving late is refused
        run_until(lambda: self.remote.responses)
        self.assertEqual(self.remote.responses, [None])
        sync.remote_clipboard_ready(self.remote)
        self.assertEqual(len(self.remote.announced), 2, "announced afresh after a reconnect")

    def test_remote_files_are_downloaded_when_pasted(self):
        sync = self.make()
        statuses = []
        sync.on_status = statuses.append
        sync.remote_clipboard_ready(self.remote)
        entries = [ClipboardFileEntry("Ordner", True, 0), ClipboardFileEntry("Ordner\\a.txt", False, 5),
                   ClipboardFileEntry("b.txt", False, 2)]
        self.remote.answers = {0xC0F0: FileGroupDescriptor.encode(entries)}
        self.remote.files = {1: b"hallo", 2: b"ok"}
        sync.remote_clipboard_formats(self.remote, [RemoteClipboardFormat(0xC0F0, "FileGroupDescriptorW"),
                                                    RemoteClipboardFormat(13)])
        run_until(lambda: sync._own_offer is not None)
        self.assertEqual(sync._own_offer.mimes, ("text/uri-list",))
        self.assertEqual(self.remote.file_requests, [], "nothing is downloaded before a paste")
        uris = read_remote(sync, "text/uri-list").decode().split("\r\n")
        folder = Path(uris[0][len("file://"):])
        self.assertEqual((folder / "a.txt").read_bytes(), b"hallo")
        self.assertEqual(Path(uris[1][len("file://"):]).read_bytes(), b"ok")
        self.assertTrue(str(folder).startswith(self.cache))
        self.assertEqual(uris[0], file_uri(folder))
        self.assertIn("Copied 2 files from the remote PC.", statuses)

    def test_local_files_are_offered_and_served(self):
        root = Path(tempfile.mkdtemp(prefix="weitblick-files-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "Ordner").mkdir()
        (root / "Ordner" / "x.bin").write_bytes(b"0123456789")
        sync = self.make()
        self.clipboard.data = {"x-special/gnome-copied-files": f"copy\n{file_uri(root / 'Ordner')}".encode(),
                               "text/uri-list": f"{file_uri(root / 'Ordner')}\r\n".encode()}
        self.clipboard.text = str(root / "Ordner")
        sync.remote_clipboard_ready(self.remote)
        run_until(lambda: self.remote.announced)
        self.assertEqual(self.remote.announced[0], [RemoteClipboardFormat(0xC000, "FileGroupDescriptorW"),
                                                    RemoteClipboardFormat(13)])
        sync.remote_clipboard_data_requested(self.remote, 0xC000)
        run_until(lambda: self.remote.responses)
        entries = FileGroupDescriptor.decode(self.remote.responses[0])
        self.assertEqual([e.path for e in entries], ["Ordner", "Ordner\\x.bin"])
        sync.remote_clipboard_file_requested(self.remote, RemoteFileRequest(7, 1, True, 0, 8))
        sync.remote_clipboard_file_requested(self.remote, RemoteFileRequest(8, 1, False, 4, 100))
        run_until(lambda: len(self.remote.file_responses) == 2)
        self.assertEqual(self.remote.file_responses[7], struct.pack("<Q", 10))
        self.assertEqual(self.remote.file_responses[8], b"456789")


if __name__ == "__main__":
    unittest.main()
