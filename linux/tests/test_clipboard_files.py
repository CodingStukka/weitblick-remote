"""Files over the clipboard (port of FileClipboardTests)."""

import os
import random
import shutil
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

from weitblick.clipboard.files import (ClipboardFileEntry, FileGroupDescriptor, LocalFileList, RemoteFileFetcher,
                                       RemoteFileTransfer, file_uri, parse_file_uris, safe_relative_path)
from weitblick.rdp.session import RemoteFileRequest


class FileGroupDescriptorTests(unittest.TestCase):
    def test_round_trip_keeps_paths_kinds_sizes_and_times(self):
        modified = 1_700_000_000.0
        entries = [
            ClipboardFileEntry("Ordner-Ä", True, 0, modified),
            ClipboardFileEntry("Ordner-Ä\\Grüße.txt", False, 12, modified),
            ClipboardFileEntry("groß.bin", False, 5_000_000_000, None),
        ]
        data = FileGroupDescriptor.encode(entries)
        self.assertEqual(len(data), 4 + 3 * 592)
        self.assertEqual(FileGroupDescriptor.decode(data), entries)

    def test_record_layout_matches_filedescriptorw(self):
        data = FileGroupDescriptor.encode([ClipboardFileEntry("a", False, 0x1_0000_0002)])
        self.assertEqual(list(data[0:4]), [1, 0, 0, 0])  # cItems
        self.assertEqual(data[4 + 36], 0x80)  # FILE_ATTRIBUTE_NORMAL
        self.assertEqual(list(data[4 + 64:4 + 72]), [1, 0, 0, 0, 2, 0, 0, 0])  # nFileSizeHigh, nFileSizeLow
        self.assertEqual(list(data[4 + 72:4 + 76]), [0x61, 0, 0, 0])  # "a", NUL

    def test_rejects_paths_outside_the_copied_items(self):
        for path in ["..\\evil.txt", "a\\..\\..\\b", "C:\\Windows\\x", "\\\\?\\c:\\x", ""]:
            data = FileGroupDescriptor.encode([ClipboardFileEntry(path, False, 1)])
            self.assertIsNone(FileGroupDescriptor.decode(data), path)
        self.assertEqual(safe_relative_path("\\a/b\\.\\c\\"), "a\\b\\c")

    def test_rejects_truncated_data(self):
        data = FileGroupDescriptor.encode([ClipboardFileEntry("a", False, 1)])
        self.assertIsNone(FileGroupDescriptor.decode(data[:100]))
        self.assertIsNone(FileGroupDescriptor.decode(b"\x01\x00"))


class FileURITests(unittest.TestCase):
    def test_parses_uri_lists_and_gnome_copied_files(self):
        uri_list = b"# comment\r\nfile:///home/u/Gr%C3%BC%C3%9Fe.txt\r\nhttps://example.com/x\r\nfile://localhost/tmp/a%20b\r\n"
        self.assertEqual(parse_file_uris(uri_list, "text/uri-list"), ["/home/u/Grüße.txt", "/tmp/a b"])
        self.assertEqual(parse_file_uris(b"cut\nfile:///tmp/x\nfile:///tmp/y", "x-special/gnome-copied-files"),
                         ["/tmp/x", "/tmp/y"])

    def test_file_uri_escapes(self):
        self.assertEqual(file_uri("/tmp/a b/Grüße#1.txt"), "file:///tmp/a%20b/Gr%C3%BC%C3%9Fe%231.txt")
        self.assertEqual(parse_file_uris(file_uri("/tmp/a b/ä#?.txt").encode(), "text/uri-list"), ["/tmp/a b/ä#?.txt"])


class LocalFileListTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="weitblick-tests-"))
        (self.root / "Ordner-Ä" / "tief").mkdir(parents=True)
        (self.root / "Ordner-Ä" / "tief" / "Grüße.txt").write_bytes(b"hallo")
        (self.root / "Ordner-Ä" / ".DS_Store").write_bytes(b"x")
        (self.root / "daten.bin").write_bytes(bytes(i % 256 for i in range(300)))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_lists_folders_recursively_before_their_contents(self):
        listed = LocalFileList.create([str(self.root / "Ordner-Ä"), str(self.root / "daten.bin")], 1000)
        self.assertEqual([e.path for e in listed.entries],
                         ["Ordner-Ä", "Ordner-Ä\\tief", "Ordner-Ä\\tief\\Grüße.txt", "daten.bin"])
        self.assertEqual([e.is_directory for e in listed.entries], [True, True, False, False])
        self.assertEqual(listed.total_size, 305)
        self.assertEqual(FileGroupDescriptor.decode(listed.descriptor)[2].size, 5)

    def test_answers_sizes_and_ranges(self):
        listed = LocalFileList.create([str(self.root / "daten.bin")], 1000)
        self.assertEqual(listed.answer(RemoteFileRequest(1, 0, True, 0, 8)), bytes([44, 1, 0, 0, 0, 0, 0, 0]))
        self.assertEqual(listed.answer(RemoteFileRequest(2, 0, False, 290, 100)), bytes(i % 256 for i in range(290, 300)))
        self.assertIsNone(listed.answer(RemoteFileRequest(3, 5, True, 0, 8)))
        listed.close()

    def test_refuses_more_than_the_limit(self):
        self.assertIsNone(LocalFileList.create([str(self.root / "daten.bin")], 299))
        self.assertEqual(LocalFileList.total_of([str(self.root / "daten.bin")]), 300)

    def test_missing_files_give_nothing(self):
        self.assertIsNone(LocalFileList.create([str(self.root / "fehlt")], 1000))


class FakeFileServer:
    """Answers requests like a server would, optionally out of order and with short answers."""

    def __init__(self, files, short_answers=False):
        self.files = files
        self.short_answers = short_answers
        self.fetcher = None
        self.lock = threading.Lock()
        self.queued = []
        self.requests = 0

    def send(self, request):
        with self.lock:
            self.requests += 1
            self.queued.append(request)
        threading.Timer(0.001, self.answer_newest_first).start()
        return True

    def answer_newest_first(self):
        with self.lock:
            if not self.queued:
                return
            request = self.queued.pop()
        content = self.files[request.file_index]
        if request.size_only:
            self.fetcher.deliver(request.stream_id, struct.pack("<Q", len(content)))
            return
        start = request.offset
        length = max(1, request.length // 3) if self.short_answers else request.length
        self.fetcher.deliver(request.stream_id, content[start:start + length])


class RemoteFileFetcherTests(unittest.TestCase):
    def test_downloads_in_order_despite_shuffled_and_short_answers(self):
        rng = random.Random(7)
        content = bytes(rng.randrange(256) for _ in range(3 * RemoteFileFetcher.CHUNK_SIZE + 123))
        for short in (False, True):
            server = FakeFileServer({0: content}, short_answers=short)
            fetcher = RemoteFileFetcher(server.send)
            server.fetcher = fetcher
            received = bytearray()
            self.assertTrue(fetcher.download(0, len(content), fetcher.generation, received.extend))
            self.assertEqual(bytes(received), content, f"short answers: {short}")
            self.assertEqual(fetcher.size(0, fetcher.generation), len(content))

    def test_reset_fails_a_download_in_progress(self):
        fetcher = RemoteFileFetcher(lambda request: True)
        threading.Timer(0.1, fetcher.reset).start()
        started = time.monotonic()
        self.assertFalse(fetcher.download(0, 10, fetcher.generation, lambda data: None))
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(fetcher.download(0, 10, fetcher.generation - 1, lambda data: None))


class RemoteFileTransferTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="weitblick-tests-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_materializes_folders_with_their_contents(self):
        entries = [
            ClipboardFileEntry("Ordner", True, 0),
            ClipboardFileEntry("Ordner\\a.txt", False, 5, 1_600_000_000.0),
            ClipboardFileEntry("Ordner\\leer", True, 0),
            ClipboardFileEntry("b.bin", False, 0),  # size unknown: asked from the server
        ]
        server = FakeFileServer({1: b"hallo", 3: b"\x01\x02\x03"})
        fetcher = RemoteFileFetcher(server.send)
        server.fetcher = fetcher
        transfer = RemoteFileTransfer(entries, fetcher, fetcher.generation, root=self.root)
        progress = []
        local = transfer.materialize_all(lambda done, total: progress.append(done))
        self.assertEqual(local, [str(transfer.directory / "Ordner"), str(transfer.directory / "b.bin")])
        self.assertEqual((transfer.directory / "Ordner" / "a.txt").read_bytes(), b"hallo")
        self.assertEqual(os.path.getmtime(transfer.directory / "Ordner" / "a.txt"), 1_600_000_000.0)
        self.assertTrue((transfer.directory / "Ordner" / "leer").is_dir())
        self.assertEqual((transfer.directory / "b.bin").read_bytes(), b"\x01\x02\x03")
        requests = server.requests
        self.assertEqual(transfer.materialize_all(), local)  # cached, no new requests
        self.assertEqual(server.requests, requests)

    def test_a_failed_download_leaves_nothing_behind(self):
        fetcher = RemoteFileFetcher(lambda request: False)
        transfer = RemoteFileTransfer([ClipboardFileEntry("x.txt", False, 3)], fetcher, fetcher.generation,
                                      root=self.root)
        self.assertIsNone(transfer.materialize_all())
        self.assertFalse((transfer.directory / "x.txt").exists())

    def test_purges_stale_downloads(self):
        old, new = self.root / "old", self.root / "new"
        old.mkdir()
        new.mkdir()
        os.utime(old, (time.time() - 3 * 86400, time.time() - 3 * 86400))
        RemoteFileTransfer.purge_stale(self.root)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())


if __name__ == "__main__":
    unittest.main()
