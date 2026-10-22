#!/bin/sh
# Installs the Flatpak bundle on a clean system and checks that it starts. Runs as root inside the Flatpak CI
# image (privileged, as CI does; never on a real desktop):
#
#   docker run --rm --privileged -v "$PWD":/src -w /src ghcr.io/flathub-infra/flatpak-github-actions:gnome-51 \
#       linux/scripts/test-flatpak.sh Weitblick-Remote.flatpak
#
# Checks: the bundle installs against the GNOME runtime, the bridge loads with the bundled FreeRDP, Tesseract
# knows English and German, the desktop file and the .rdp MIME type are exported, and the app opens its window
# on a private Xvfb display.
set -eu
bundle=$(realpath "$1")
app=nrw.neuhaus.WeitblickRemote
# The runtime is part of the CI image: no updates from Flathub.
flatpak install -y --noninteractive --system --no-deps --no-related --bundle "$bundle"
flatpak info "$app"

flatpak run "$app" --version
flatpak run --env=PYTHONPATH=/app/lib/weitblick-remote --command=python3 "$app" - <<'PYTHON'
from weitblick import bridge, paths
from weitblick.agent import ocr
bridge.lib()
assert paths.locale_dir() is not None, "translation missing"
assert (paths.icon_dir() / "hicolor" / "scalable" / "apps" / "nrw.neuhaus.WeitblickRemote.svg").is_file()
assert ocr.tesseract_path() == "/app/bin/tesseract", ocr.tesseract_path()
assert ocr._language_argument(ocr.tesseract_path()) == "deu+eng", "Tesseract languages missing"

# A line of text, rendered and recognized.
import io
import cairo
import gi
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo
surface = cairo.ImageSurface(cairo.FORMAT_RGB24, 640, 80)
context = cairo.Context(surface)
context.set_source_rgb(1, 1, 1)
context.paint()
context.set_source_rgb(0, 0, 0)
layout = PangoCairo.create_layout(context)
layout.set_font_description(Pango.FontDescription.from_string("Sans 28"))
layout.set_text("Weitblick Remote Übersicht", -1)
context.move_to(12, 16)
PangoCairo.show_layout(context, layout)
png = io.BytesIO()
surface.write_to_png(png)
text = " ".join(line.text for line in ocr.recognize(png.getvalue()))
assert "Weitblick Remote Übersicht" in text, text
print("bridge, translation, icons and text recognition: ok")
PYTHON
flatpak run --command=sh "$app" -c 'test -x /app/bin/weitblick-mcp && command -v flatpak-spawn >/dev/null'
exports=/var/lib/flatpak/exports/share
grep -q "^Exec=.*flatpak run.*$app" "$exports/applications/$app.desktop"
test -f "$exports/mime/packages/$app.xml"
test -f "$exports/icons/hicolor/scalable/apps/$app.svg"

export XDG_RUNTIME_DIR="$(mktemp -d)"
chmod 700 "$XDG_RUNTIME_DIR"
home=$(mktemp -d)
Xvfb :90 -screen 0 1280x800x24 -nolisten tcp >/dev/null 2>&1 &
xvfb=$!
sleep 1
HOME=$home DISPLAY=:90 LANG=de_DE.UTF-8 LANGUAGE=de dbus-run-session -- flatpak run "$app" >"$home/app.log" 2>&1 &
started=$!
sleep 8
kill -0 $started || { cat "$home/app.log"; echo "the app exited"; exit 1; }
kill $started $xvfb 2>/dev/null || true
grep -i 'traceback\|critical' "$home/app.log" && { cat "$home/app.log"; exit 1; }
echo "flatpak ok"
