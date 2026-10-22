"""Text recognition for read_screen and find_text with the Tesseract command-line tool (the macOS app
uses Apple Vision). German and English when both language packs are installed. Port of
ScreenReader.swift: the caller hands in a PNG of desktop pixels (upscaled by `upscale_factor` for
small UI text) and gets lines with boxes in desktop pixels.

    lines = recognize(png, offset=(region.x, region.y), scale=factor)   # blocking: run off the main loop
    recognize_async(png, done, offset=..., scale=...)                    # done(lines, error) on the main loop
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable

INSTALL_HINT = "sudo apt install tesseract-ocr tesseract-ocr-deu"

#: Sparse text: UI screenshots are labels and buttons scattered over the screen, not book pages.
PAGE_SEGMENTATION_MODE = "11"


class OCRUnavailable(RuntimeError):
    """Tesseract is not installed."""


class OCRFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class Word:
    text: str
    #: (x, y, width, height) in desktop pixels.
    box: tuple[float, float, float, float]
    #: 0…1.
    confidence: float
    #: Where the word sits in its line's text: line.text[start:end].
    start: int
    end: int


@dataclass
class Line:
    """One recognized line: its text and where it is (desktop pixels)."""

    text: str
    box: tuple[float, float, float, float]
    confidence: float
    words: list[Word] = field(default_factory=list)

    def box_of_range(self, start: int, end: int) -> tuple[float, float, float, float] | None:
        """The box of text[start:end], for matches inside a line. Partly covered words are cut
        proportionally (Tesseract only knows word boxes)."""
        boxes = []
        for word in self.words:
            lo, hi = max(start, word.start), min(end, word.end)
            if lo >= hi:
                continue
            x, y, width, height = word.box
            per_char = width / max(1, word.end - word.start)
            boxes.append((x + (lo - word.start) * per_char, y, (hi - lo) * per_char, height))
        if not boxes:
            return None
        return _union(boxes)


def _union(boxes: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    min_x = min(box[0] for box in boxes)
    min_y = min(box[1] for box in boxes)
    max_x = max(box[0] + box[2] for box in boxes)
    max_y = max(box[1] + box[3] for box in boxes)
    return (min_x, min_y, max_x - min_x, max_y - min_y)


def upscale_factor(width: int, height: int) -> int:
    """Small UI text reads better at twice the size; large desktops are read as they are."""
    return 2 if max(width, height) <= 2000 else 1


def parse_tsv(tsv: str, offset: tuple[float, float] = (0, 0), scale: float = 1.0) -> list[Line]:
    """Tesseract's TSV output as lines, top to bottom. Image pixels are divided by `scale` (the
    upscale factor) and moved by `offset` (the region's origin) into desktop pixels."""
    grouped: dict[tuple[str, str, str, str], list[tuple[str, tuple[float, float, float, float], float]]] = {}
    for row in tsv.splitlines()[1:]:
        columns = row.split("\t")
        if len(columns) < 12 or columns[0] != "5":
            continue
        text = columns[11].strip()
        if not text:
            continue
        try:
            left, top, width, height = (float(value) for value in columns[6:10])
            confidence = float(columns[10])
        except ValueError:
            continue
        box = (offset[0] + left / scale, offset[1] + top / scale, width / scale, height / scale)
        grouped.setdefault(tuple(columns[1:5]), []).append((text, box, max(0.0, confidence) / 100))
    lines = []
    for words in grouped.values():
        words.sort(key=lambda word: word[1][0])
        placed, cursor = [], 0
        for text, box, confidence in words:
            if placed:
                cursor += 1  # the space between words
            placed.append(Word(text, box, confidence, cursor, cursor + len(text)))
            cursor += len(text)
        lines.append(Line(text=" ".join(word.text for word in placed),
                          box=_union([word.box for word in placed]),
                          confidence=sum(word.confidence for word in placed) / len(placed),
                          words=placed))
    lines.sort(key=lambda line: (line.box[1], line.box[0]))
    return lines


_languages_lock = threading.Lock()
_languages: str | None = None


def tesseract_path() -> str | None:
    return shutil.which("tesseract")


def _language_argument(binary: str) -> str | None:
    """"deu+eng" when both are installed, else whatever of the two is there (None: Tesseract's default)."""
    global _languages
    with _languages_lock:
        if _languages is None:
            try:
                listed = subprocess.run([binary, "--list-langs"], capture_output=True, text=True, timeout=10)
                installed = set(listed.stdout.split())
            except (OSError, subprocess.SubprocessError):
                installed = set()
            _languages = "+".join(lang for lang in ("deu", "eng") if lang in installed)
        return _languages or None


def recognize(png: bytes, offset: tuple[float, float] = (0, 0), scale: float = 1.0,
              timeout: float = 60) -> list[Line]:
    """Recognizes the text in a PNG. Blocks (Tesseract takes about a second): call it off the main loop."""
    binary = tesseract_path()
    if binary is None:
        raise OCRUnavailable(f"Text recognition needs Tesseract, which isn't installed. Install it with: {INSTALL_HINT}")
    command = [binary, "stdin", "stdout", "--psm", PAGE_SEGMENTATION_MODE]
    languages = _language_argument(binary)
    if languages:
        command += ["-l", languages]
    command.append("tsv")
    try:
        result = subprocess.run(command, input=png, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise OCRFailed(f"Tesseract took longer than {int(timeout)} seconds.") from error
    except OSError as error:
        raise OCRFailed(f"Tesseract couldn't be started: {error}") from error
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise OCRFailed(f"Tesseract failed: {message[-1] if message else f'exit status {result.returncode}'}")
    return parse_tsv(result.stdout.decode("utf-8", "replace"), offset, scale)


def recognize_async(png: bytes, done: Callable[[list[Line] | None, Exception | None], None],
                    offset: tuple[float, float] = (0, 0), scale: float = 1.0) -> None:
    """Runs recognize() on a thread; `done(lines, error)` is called on the GLib main loop."""
    from gi.repository import GLib

    def work() -> None:
        try:
            lines, error = recognize(png, offset, scale), None
        except Exception as failure:  # noqa: BLE001 - handed to the caller
            lines, error = None, failure
        GLib.idle_add(lambda: (done(lines, error), GLib.SOURCE_REMOVE)[1])

    threading.Thread(target=work, name="weitblick-ocr", daemon=True).start()
