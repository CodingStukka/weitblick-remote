"""CF_UNICODETEXT: UTF-16LE, CRLF line ends, NUL-terminated."""


def encode(text: str) -> bytes:
    """Any line ending (LF, CR, CRLF) becomes CRLF."""
    windows = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    return windows.encode("utf-16-le", "surrogatepass") + b"\0\0"


def decode(data: bytes) -> str:
    """Reads up to the first NUL character; CRLF becomes LF."""
    length = len(data) & ~1
    for offset in range(0, length, 2):
        if data[offset] == 0 and data[offset + 1] == 0:
            length = offset
            break
    # Lenient: an unpaired surrogate becomes U+FFFD instead of failing the whole text.
    return data[:length].decode("utf-16-le", "replace").replace("\r\n", "\n")
