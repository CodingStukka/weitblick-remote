#!/bin/sh
# Development only: a local RDP server to test against without a Windows PC. Runs FreeRDP's shadow
# server on a private Xvfb display (never the real desktop), on 127.0.0.1 only, without
# authentication (any user name and password are accepted).
#
#   scripts/dev-test-server.sh [port]      default 33890; Ctrl+C stops it
#
# The packages (xvfb, freerdp3-shadow-x11 and its libraries) are downloaded into build/testserver
# without root on the first run.
set -eu
port=${1:-33890}
display=${WB_TEST_DISPLAY:-:97}
root=$(cd "$(dirname "$0")/.." && pwd)/build/testserver
lib=$root/usr/lib/x86_64-linux-gnu

if [ ! -x "$root/usr/bin/freerdp-shadow-cli3" ] || [ ! -x "$root/usr/bin/Xvfb" ]; then
    tmp=$(mktemp -d)
    (cd "$tmp" && apt-get download xvfb freerdp3-shadow-x11 libfreerdp-shadow3-3 \
        libfreerdp-shadow-subsystem3-3 libwinpr-tools3-3)
    mkdir -p "$root"
    for deb in "$tmp"/*.deb; do dpkg-deb -x "$deb" "$root"; done
    rm -rf "$tmp"
    for so in "$lib"/*.so.3.*; do ln -sf "$(basename "$so")" "${so%.*.*}"; done
fi

"$root/usr/bin/Xvfb" "$display" -screen 0 1280x800x24 -nolisten tcp &
xvfb=$!
trap 'kill $xvfb 2>/dev/null; kill $app 2>/dev/null' EXIT INT TERM
sleep 1
# Something to look at and type into.
app=
if command -v xterm >/dev/null; then
    DISPLAY=$display xterm -geometry 100x30+40+40 & app=$!
elif command -v gnome-text-editor >/dev/null; then
    DISPLAY=$display GDK_BACKEND=x11 XDG_CONFIG_HOME=$root/config gnome-text-editor --standalone & app=$!
fi
# XDG_CONFIG_HOME keeps the shadow server's certificate out of ~/.config/freerdp.
mkdir -p "$root/config"
DISPLAY=$display XDG_CONFIG_HOME=$root/config LD_LIBRARY_PATH=$lib "$root/usr/bin/freerdp-shadow-cli3" \
    /bind-address:127.0.0.1 /port:"$port" -auth
