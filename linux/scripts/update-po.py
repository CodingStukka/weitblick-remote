#!/usr/bin/env python3
"""Extracts the translatable strings (_(), ngettext(), N_()) from weitblick/ into po/weitblick-remote.pot
and merges them into every po/*.po: existing translations stay, new strings come in untranslated,
strings no longer used are dropped.

    scripts/update-po.py
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADER = ('msgid ""\nmsgstr ""\n"Project-Id-Version: weitblick-remote\\n"\n"MIME-Version: 1.0\\n"\n'
          '"Content-Type: text/plain; charset=UTF-8\\n"\n"Content-Transfer-Encoding: 8bit\\n"\n')


def extract() -> dict[tuple[str, str | None], list[str]]:
    found: dict[tuple[str, str | None], list[str]] = {}
    for file in sorted((ROOT / "weitblick").rglob("*.py")):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            args = node.args
            strings = [a.value for a in args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            where = f"{file.relative_to(ROOT)}:{node.lineno}"
            if node.func.id in ("_", "N_") and strings:
                found.setdefault((strings[0], None), []).append(where)
            elif node.func.id == "ngettext" and len(strings) >= 2:
                found.setdefault((strings[0], strings[1]), []).append(where)
    return found


def quote(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def read_po(path: Path) -> dict[tuple[str, str | None], list[str]]:
    import ast as _ast
    entries: dict[tuple[str, str | None], list[str]] = {}
    current: dict[str, str] = {}
    key = None

    def flush():
        if "msgid" in current and current["msgid"]:
            plural = current.get("msgid_plural")
            if plural is None:
                entries[(current["msgid"], None)] = [current.get("msgstr", "")]
            else:
                entries[(current["msgid"], plural)] = [current.get(f"msgstr[{i}]", "") for i in range(2)]
        current.clear()

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith('"'):
            current[key] += _ast.literal_eval(line)
            continue
        name, _sep, value = line.partition(" ")
        if name == "msgid":
            flush()
        key = name
        current[key] = _ast.literal_eval(value)
    flush()
    return entries


def write(path: Path, strings, translations=None) -> None:
    lines = [HEADER + ('"Plural-Forms: nplurals=2; plural=(n != 1);\\n"\n' if translations is not None else "")]
    for (msgid, plural), places in sorted(strings.items(), key=lambda item: item[1][0]):
        lines.append("#: " + " ".join(places[:3]))
        lines.append(f"msgid {quote(msgid)}")
        old = (translations or {}).get((msgid, plural))
        if plural is None:
            lines.append(f"msgstr {quote(old[0] if old else '')}")
        else:
            lines.append(f"msgid_plural {quote(plural)}")
            for index in range(2):
                lines.append(f"msgstr[{index}] {quote(old[index] if old else '')}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    strings = extract()
    write(ROOT / "po" / "weitblick-remote.pot", strings)
    for po in sorted((ROOT / "po").glob("*.po")):
        translations = read_po(po)
        write(po, strings, translations)
        missing = sum(1 for key in strings if not all(translations.get(key, [""])))
        print(f"{po.name}: {len(strings)} strings, {missing} untranslated")
    print(f"weitblick-remote.pot: {len(strings)} strings")
