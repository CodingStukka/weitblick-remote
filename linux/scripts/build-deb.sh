#!/bin/sh
# Builds the Debian package after `make`: build/deb/weitblick-remote_<version>_<arch>.deb
#
#   scripts/build-deb.sh            (or: make deb)
#
# Needs dpkg-dev (dpkg-deb, dpkg-shlibdeps; build-essential pulls it in). The package installs into /usr
# like `make install PREFIX=/usr` and depends on the distribution's GTK 4, libadwaita and FreeRDP 3, so it
# is for Ubuntu 24.04, Debian 13 and newer.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
out=$here/build/deb
stage=$out/stage
package=weitblick-remote
version=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$here/weitblick/__init__.py")
arch=$(dpkg --print-architecture)
maintainer=${DEB_MAINTAINER:-"Lukas Mechsner <lukas.mechsner@googlemail.com>"}

[ -f "$here/build/libweitblick-bridge.so" ] || { echo "Build first: make" >&2; exit 1; }
[ -n "$version" ] || { echo "No VERSION in weitblick/__init__.py" >&2; exit 1; }

rm -rf "$out"
mkdir -p "$stage/DEBIAN"
DESTDIR="$stage" "$here/scripts/install.sh" /usr

doc=$stage/usr/share/doc/$package
mkdir -p "$doc"
cat > "$doc/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: Weitblick Remote
Source: https://github.com/mneuhaus/weitblick-remote

Files: *
Copyright: 2026 Marc Neuhaus and contributors
License: Apache-2.0

License: Apache-2.0
 On Debian systems, the full text of the Apache License 2.0 is in
 /usr/share/common-licenses/Apache-2.0. Third-party software (FreeRDP, GTK, libadwaita, libsecret,
 Tesseract) is used from the distribution's packages; see /usr/lib/weitblick-remote/THIRD_PARTY_NOTICES.md.
EOF

bridge=$stage/usr/lib/$package/weitblick/lib/libweitblick-bridge.so
strip --strip-unneeded --remove-section=.comment --remove-section=.note "$bridge"

# Library dependencies of the bridge (FreeRDP, WinPR, libc) as dpkg-shlibdeps finds them.
mkdir -p "$out/shlibs/debian"
printf 'Source: %s\n\nPackage: %s\nArchitecture: any\n' "$package" "$package" > "$out/shlibs/debian/control"
shlibs=$(cd "$out/shlibs" && dpkg-shlibdeps -O "$bridge" |
         sed -n 's/^shlibs:Depends=//p')

cat > "$stage/DEBIAN/control" <<EOF
Package: $package
Version: $version
Architecture: $arch
Maintainer: $maintainer
Installed-Size: $(du -sk --exclude=DEBIAN "$stage" | cut -f1)
Depends: $shlibs, python3 (>= 3.11), python3-gi, gir1.2-glib-2.0, gir1.2-gtk-4.0 (>= 4.14), gir1.2-adw-1 (>= 1.5), gir1.2-secret-1
Recommends: tesseract-ocr
Suggests: tesseract-ocr-deu
Section: net
Priority: optional
Homepage: https://mneuhaus.github.io/weitblick-remote/
Description: Remote Desktop for Windows PCs
 Weitblick Remote connects to Windows PCs over RDP: tabs, a keyboard made for
 Windows, the clipboard with files and folders, shared folders, sound,
 microphone, printers and auto-reconnect. Imports connections from Remmina,
 Jump Desktop and .rdp files, keeps passwords in the keyring and lets AI agents
 (MCP) work in a session. Built on FreeRDP 3, GTK 4 and libadwaita.
EOF

# Byte-compiled on the target (the Python version there may differ), removed again before removal.
cat > "$stage/DEBIAN/postinst" <<EOF
#!/bin/sh
set -e
if [ "\$1" = configure ] && command -v python3 >/dev/null; then
    python3 -m compileall -q /usr/lib/$package/weitblick >/dev/null 2>&1 || true
fi
EOF
cat > "$stage/DEBIAN/prerm" <<EOF
#!/bin/sh
set -e
find /usr/lib/$package -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
EOF

find "$stage" -type d -exec chmod 0755 {} +
find "$stage" -type f -exec chmod 0644 {} +
chmod 0755 "$stage/usr/bin/"* "$stage/DEBIAN/postinst" "$stage/DEBIAN/prerm"

deb=$out/${package}_${version}_${arch}.deb
dpkg-deb --root-owner-group -Zxz --build "$stage" "$deb" >/dev/null
rm -rf "$out/shlibs"
echo "$deb"
