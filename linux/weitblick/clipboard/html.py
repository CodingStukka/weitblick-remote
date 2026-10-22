"""The Windows "HTML Format" (CF_HTML): a header with UTF-8 byte offsets, then the document with the
copied fragment between <!--StartFragment--> and <!--EndFragment-->."""

import re

START_MARKER = "<!--StartFragment-->"
END_MARKER = "<!--EndFragment-->"
_META = '<meta charset="utf-8">'


def _header(start_html: int, end_html: int, start_fragment: int, end_fragment: int) -> str:
    return (f"Version:0.9\r\nStartHTML:{start_html:010d}\r\nEndHTML:{end_html:010d}\r\n"
            f"StartFragment:{start_fragment:010d}\r\nEndFragment:{end_fragment:010d}\r\n")


def encode(html: str) -> bytes:
    """Wraps local HTML (a fragment or a whole document) into CF_HTML; the body is the fragment."""
    prefix, fragment, suffix = _split_at_body(html)
    before = (prefix + START_MARKER).encode("utf-8")
    middle = fragment.encode("utf-8")
    document = before + middle + (END_MARKER + suffix).encode("utf-8")
    header_length = len(_header(0, 0, 0, 0).encode("ascii"))  # fixed width
    header = _header(header_length, header_length + len(document),
                     header_length + len(before), header_length + len(before) + len(middle))
    return header.encode("ascii") + document + b"\0"


def decode(data: bytes) -> str | None:
    """The HTML document of a CF_HTML blob (StartHTML…EndHTML, else the fragment, else everything
    after the header), with a UTF-8 charset declaration so local apps decode it correctly."""
    nul = data.find(b"\0")
    raw = data if nul < 0 else data[:nul]
    offsets = _header_offsets(raw)
    start, end = offsets.get("StartHTML"), offsets.get("EndHTML")
    fragment_start, fragment_end = offsets.get("StartFragment"), offsets.get("EndFragment")
    if start is not None and end is not None and 0 <= start < end <= len(raw):
        selected = raw[start:end]
    elif fragment_start is not None and fragment_end is not None and 0 <= fragment_start <= fragment_end <= len(raw):
        selected = raw[fragment_start:fragment_end]
    else:
        first = raw.find(b"<")
        if first < 0:
            return None
        selected = raw[first:]
    return _with_utf8_charset(selected.decode("utf-8", "replace"))


def _split_at_body(html: str) -> tuple[str, str, str]:
    body_open = re.search(r"<body", html, re.IGNORECASE)
    if body_open:
        tag_end = html.find(">", body_open.end())
        closes = [m.start() for m in re.finditer(r"</body", html, re.IGNORECASE)]
        if tag_end >= 0 and closes and tag_end + 1 <= closes[-1]:
            return html[:tag_end + 1], html[tag_end + 1:closes[-1]], html[closes[-1]:]
    return "<html><body>", html, "</body></html>"


def _header_offsets(raw: bytes) -> dict[str, int]:
    """`Name:Value` lines before the document; values are byte offsets (-1 = absent)."""
    header_end = raw.find(b"<")
    header = raw[: header_end if header_end >= 0 else len(raw)].decode("utf-8", "replace")
    offsets: dict[str, int] = {}
    for line in header.splitlines():
        name, separator, value = line.partition(":")
        if separator:
            try:
                offsets[name] = int(value.strip())
            except ValueError:
                pass
    return offsets


def _with_utf8_charset(html: str) -> str:
    if "charset" in html.lower():
        return html
    head = re.search(r"<head", html, re.IGNORECASE)
    if head:
        tag_end = html.find(">", head.end())
        if tag_end >= 0:
            return html[:tag_end + 1] + _META + html[tag_end + 1:]
    return _META + html
