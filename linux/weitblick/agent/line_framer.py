"""Splits a byte stream into newline-delimited messages, the MCP stdio framing (port of
LineFramer.swift): "\\n" ends a message, a "\\r" before it is dropped, empty lines are skipped."""

from __future__ import annotations


class LineTooLong(Exception):
    """A message grew beyond max_line_length bytes without a newline."""

    def __init__(self, size: int):
        super().__init__(f"line too long ({size} bytes)")
        self.size = size


class LineFramer:
    def __init__(self, max_line_length: int = 32 << 20):
        self.max_line_length = max_line_length
        self._buffer = bytearray()

    def append(self, data: bytes) -> list[str]:
        """Feeds bytes and returns the complete lines they finish, in order. Lines are decoded as
        UTF-8 only once complete, so characters split across chunks survive."""
        self._buffer += data
        lines: list[str] = []
        while (newline := self._buffer.find(b"\n")) >= 0:
            line = bytes(self._buffer[:newline])
            del self._buffer[:newline + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            if line:
                lines.append(line.decode("utf-8", "replace"))
        if len(self._buffer) > self.max_line_length:
            size = len(self._buffer)
            self._buffer = bytearray()
            raise LineTooLong(size)
        return lines

    @property
    def pending_byte_count(self) -> int:
        """Bytes waiting for their newline."""
        return len(self._buffer)
