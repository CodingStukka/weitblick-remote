#!/usr/bin/env python3
"""Compiles a gettext .po file into a .mo file (msgfmt without the gettext tools).

    scripts/msgfmt.py po/de.po build/locale/de/LC_MESSAGES/weitblick-remote.mo

Handles msgctxt-free catalogs with plural forms; fuzzy and untranslated entries are left out.
"""

import ast
import struct
import sys
from pathlib import Path


def parse(path: Path) -> dict[bytes, bytes]:
    messages: dict[bytes, bytes] = {}
    entry: dict[str, object] = {}
    key = None
    fuzzy = False

    def flush() -> None:
        nonlocal entry, fuzzy
        msgid = entry.get("msgid")
        if msgid is not None and not fuzzy:
            if "msgid_plural" in entry:
                forms = [entry.get(f"msgstr[{i}]", "") for i in range(10) if f"msgstr[{i}]" in entry]
                if all(forms):
                    key_bytes = f"{msgid}\0{entry['msgid_plural']}".encode()
                    messages[key_bytes] = "\0".join(forms).encode()
            elif entry.get("msgstr") or msgid == "":
                messages[str(msgid).encode()] = str(entry.get("msgstr", "")).encode()
        entry = {}
        fuzzy = False

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#,") and "fuzzy" in line:
            if "msgid" in entry:
                flush()
            fuzzy = True
            continue
        if not line or line.startswith("#"):
            if not line and "msgid" in entry:
                flush()
            continue
        if line.startswith('"'):
            entry[key] = entry[key] + ast.literal_eval(line)
            continue
        name, _sep, value = line.partition(" ")
        if name == "msgid" and "msgid" in entry:
            flush()
        key = name
        entry[key] = ast.literal_eval(value)
    if "msgid" in entry:
        flush()
    return messages


def write_mo(messages: dict[bytes, bytes], path: Path) -> None:
    keys = sorted(messages)
    ids = b""
    strs = b""
    offsets = []
    for key in keys:
        offsets.append((len(ids), len(key), len(strs), len(messages[key])))
        ids += key + b"\0"
        strs += messages[key] + b"\0"
    start_ids = 7 * 4 + 16 * len(keys)
    start_strs = start_ids + len(ids)
    key_table = []
    value_table = []
    for id_offset, id_length, str_offset, str_length in offsets:
        key_table += [id_length, id_offset + start_ids]
        value_table += [str_length, str_offset + start_strs]
    output = struct.pack("Iiiiiii", 0x950412DE, 0, len(keys), 7 * 4, 7 * 4 + len(keys) * 8, 0, 0)
    output += struct.pack(f"{len(key_table)}i", *key_table)
    output += struct.pack(f"{len(value_table)}i", *value_table)
    output += ids + strs
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(output)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    write_mo(parse(Path(sys.argv[1])), Path(sys.argv[2]))
