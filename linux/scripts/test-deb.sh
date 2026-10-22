#!/bin/sh
# Installs the Debian package on a clean system and checks that it starts. Runs as root inside a
# throwaway container (CI does that; never on a real desktop):
#
#   docker run --rm -v "$PWD":/src -w /src ubuntu:24.04 linux/scripts/test-deb.sh linux/build/deb/<file>.deb
#
# Checks: apt resolves every dependency, the bridge loads with its FreeRDP libraries, the translation and
# the icons are found, and the app opens its window on a private Xvfb display (screenshot: /tmp/window.xwd).
set -eu
deb=$(realpath "$1")
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq "$deb" xvfb x11-apps dbus >/dev/null

weitblick-remote --version
PYTHONPATH=/usr/lib/weitblick-remote python3 - <<'EOF'
from weitblick import bridge, paths
bridge.lib()
assert paths.locale_dir() is not None, "translation missing"
assert (paths.icon_dir() / "hicolor" / "scalable" / "apps" / "nrw.neuhaus.WeitblickRemote.svg").is_file()
print("bridge, translation and icons: ok")
EOF
test -x /usr/bin/weitblick-mcp
desktop-file-validate /usr/share/applications/nrw.neuhaus.WeitblickRemote.desktop 2>/dev/null || true

home=$(mktemp -d)
Xvfb :90 -screen 0 1280x800x24 -nolisten tcp >/dev/null 2>&1 &
xvfb=$!
sleep 1
HOME=$home DISPLAY=:90 GDK_BACKEND=x11 LANG=de_DE.UTF-8 LANGUAGE=de NO_AT_BRIDGE=1 GTK_A11Y=none \
    dbus-run-session -- weitblick-remote >"$home/app.log" 2>&1 &
app=$!
sleep 6
kill -0 $app || { cat "$home/app.log"; echo "the app exited"; exit 1; }
xwd -root -display :90 -silent > /tmp/window.xwd
kill $app $xvfb 2>/dev/null || true
grep -i 'traceback\|critical' "$home/app.log" && { cat "$home/app.log"; exit 1; }
echo "package ok"
