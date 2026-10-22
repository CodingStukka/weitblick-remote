#!/bin/sh
# Installs Weitblick Remote into a prefix (default ~/.local; no root needed there).
#
#   scripts/install.sh [PREFIX]              after `make`
#   scripts/install.sh --uninstall [PREFIX]
#   DESTDIR=staging scripts/install.sh /usr  for a package (files under staging/usr, paths for /usr)
#
# Layout: PREFIX/lib/weitblick-remote/weitblick/ (the Python package with lib/, data/ and locale/),
# PREFIX/bin/weitblick-remote and weitblick-mcp, the desktop file, AppStream metadata, icons and the .rdp MIME
# type. Files outside lib/ and bin/ are named after the app ID, as Flatpak needs for exporting them.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
uninstall=false
if [ "${1:-}" = "--uninstall" ]; then uninstall=true; shift; fi
prefix=${1:-$HOME/.local}
dest=${DESTDIR:-}
app_id=nrw.neuhaus.WeitblickRemote
libdir=$prefix/lib/weitblick-remote
bindir=$dest$prefix/bin
share=$dest$prefix/share

refresh() {
    # Packages leave this to the triggers of desktop-file-utils, shared-mime-info and the icon cache.
    [ -z "$dest" ] || return 0
    command -v update-desktop-database >/dev/null && update-desktop-database -q "$share/applications" || true
    command -v update-mime-database >/dev/null && update-mime-database "$share/mime" >/dev/null 2>&1 || true
    command -v gtk4-update-icon-cache >/dev/null && gtk4-update-icon-cache -q -t -f "$share/icons/hicolor" 2>/dev/null || true
}

if $uninstall; then
    rm -rf "$dest$libdir"
    rm -f "$bindir/weitblick-remote" "$bindir/weitblick-mcp" "$share/applications/$app_id.desktop" \
        "$share/metainfo/$app_id.metainfo.xml" "$share/mime/packages/$app_id.xml" \
        "$share/mime/packages/weitblick-remote.xml"
    find "$share/icons/hicolor" -name "$app_id.*" -delete 2>/dev/null || true
    refresh
    echo "Weitblick Remote removed from $prefix (connections and settings in ~/.config/weitblick-remote stay)."
    exit 0
fi

[ -f "$here/build/libweitblick-bridge.so" ] && [ -d "$here/build/icons" ] || { echo "Build first: make" >&2; exit 1; }

target=$dest$libdir
rm -rf "$target"
mkdir -p "$target" "$bindir" "$share/applications" "$share/metainfo" "$share/mime/packages"
# The package without caches, plus the bridge, data and translations inside it.
(cd "$here" && find weitblick -name '__pycache__' -prune -o -type f -name '*.py' -print) | while read -r file; do
    mkdir -p "$target/$(dirname "$file")"
    cp "$here/$file" "$target/$file"
done
mkdir -p "$target/weitblick/lib" "$target/weitblick/data"
cp "$here/build/libweitblick-bridge.so" "$target/weitblick/lib/"
cp -r "$here/build/icons" "$here/data/style.css" "$target/weitblick/data/"
[ -d "$here/build/locale" ] && cp -r "$here/build/locale" "$target/weitblick/locale"
cp "$here/../LICENSE" "$here/THIRD_PARTY_NOTICES.md" "$target/" 2>/dev/null || true

for tool in weitblick-remote:weitblick weitblick-mcp:weitblick.mcp_helper; do
    name=${tool%%:*}
    module=${tool#*:}
    cat > "$bindir/$name" <<LAUNCHER
#!/bin/sh
PYTHONPATH="$libdir\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m $module "\$@"
LAUNCHER
    chmod +x "$bindir/$name"
done

sed "s|@BINDIR@|$prefix/bin|" "$here/data/$app_id.desktop.in" > "$share/applications/$app_id.desktop"
cp "$here/data/$app_id.metainfo.xml" "$share/metainfo/"
cp "$here/data/weitblick-remote-mime.xml" "$share/mime/packages/$app_id.xml"
(cd "$here/build/icons" && find hicolor -type f -name "$app_id.*") | while read -r icon; do
    mkdir -p "$share/icons/$(dirname "$icon")"
    cp "$here/build/icons/$icon" "$share/icons/$icon"
done
refresh
[ -z "$dest" ] || exit 0
echo "Installed Weitblick Remote into $prefix."
case ":$PATH:" in *":$bindir:"*) ;; *) echo "Note: $bindir is not in PATH." ;; esac
