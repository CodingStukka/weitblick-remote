#!/bin/sh
# Downloads the FreeRDP 3 and WinPR 3 -dev packages matching the installed runtime libraries and unpacks
# them into a private sysroot (default build/sysroot), so the bridge builds without root.
set -eu
dest=${1:-build/sysroot}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
(cd "$tmp" && apt-get download freerdp3-dev libwinpr3-dev)
mkdir -p "$dest"
for deb in "$tmp"/*.deb; do
    dpkg-deb -x "$deb" "$dest"
done
echo "FreeRDP headers in $dest/usr/include"
