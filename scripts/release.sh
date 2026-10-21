#!/usr/bin/env bash
# Builds the release: Release configuration (arm64, macOS 14+), signed ad-hoc with the hardened runtime
# whatever Local.xcconfig says, then packaged as
#   build/release/Weitblick-Remote.dmg   (drag-to-Applications layout)
#   build/release/Weitblick-Remote.zip
#   build/release/SHA256SUMS.txt
# The asset names carry no version on purpose: the website links to releases/latest/download/<name>.
#
#   scripts/release.sh <version>      (must match MARKETING_VERSION in project.yml)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-}"
[[ -n "$VERSION" ]] || { echo "usage: scripts/release.sh <version>" >&2; exit 2; }
cd "$ROOT"

# Parallel agents share this checkout: one build at a time (same lock as build.sh).
if [[ -z "${WEITBLICK_BUILD_LOCKED:-}" ]]; then
  export WEITBLICK_BUILD_LOCKED=1
  exec lockf -t 1800 /tmp/weitblick-build.lock "$0" "$@"
fi

# Every target (app, weitblick-mcp) carries its own MARKETING_VERSION; all must match.
PROJECT_VERSIONS=$(sed -n 's/^ *MARKETING_VERSION: "\(.*\)"$/\1/p' project.yml | sort -u | paste -sd ' ' -)
[[ "$VERSION" == "$PROJECT_VERSIONS" ]] || {
  echo "version $VERSION does not match MARKETING_VERSION ($PROJECT_VERSIONS) in project.yml" >&2; exit 1; }

APP_NAME="Weitblick Remote"
OUT="$ROOT/build/release"
APP="$OUT/$APP_NAME.app"
ENTITLEMENTS="$ROOT/Sources/WeitblickRemote/Support/WeitblickRemote.entitlements"

"$ROOT/scripts/build-freerdp.sh"
xcodegen generate --quiet
# Unsigned build; the copy below is signed here, so a personal identity never ends up in a release.
xcodebuild -project WeitblickRemote.xcodeproj -scheme WeitblickRemote -configuration Release \
  -destination generic/platform=macOS -derivedDataPath build/DerivedData -quiet CODE_SIGNING_ALLOWED=NO build

rm -rf "$OUT"
mkdir -p "$OUT"
ditto "build/DerivedData/Build/Products/Release/$APP_NAME.app" "$APP"

BUNDLE_VERSION=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$APP/Contents/Info.plist")
[[ "$BUNDLE_VERSION" == "$VERSION" ]] || { echo "built app has version $BUNDLE_VERSION, expected $VERSION" >&2; exit 1; }

# Inside-out: nested code first (deepest paths first), the app bundle last.
MAIN="$APP/Contents/MacOS/$APP_NAME"
while IFS= read -r -d '' item; do
  [[ "$item" == "$MAIN" ]] && continue
  if [[ -d "$item" ]] || file -b "$item" | grep -q 'Mach-O'; then
    codesign --force --sign - --options runtime --timestamp=none "$item"
  fi
done < <(find "$APP/Contents" -depth \( -type d \( -name '*.framework' -o -name '*.bundle' -o -name '*.xpc' \
  -o -name '*.appex' -o -name '*.app' \) -o -type f -perm -u+x -o -type f -name '*.dylib' \) -print0)
codesign --force --sign - --options runtime --timestamp=none --entitlements "$ENTITLEMENTS" "$APP"
codesign --verify --strict --deep --verbose=2 "$APP"
codesign --display --verbose=2 "$APP" 2>&1 | grep -E '^(Identifier|Format|CodeDirectory|Signature)'

# DMG: the app next to a link to /Applications.
STAGING="$(mktemp -d "${TMPDIR:-/tmp}/weitblick-dmg.XXXXXX")"
trap 'rm -rf "$STAGING"' EXIT
ditto "$APP" "$STAGING/$APP_NAME.app"
ln -s /Applications "$STAGING/Applications"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGING" -fs HFS+ -format UDZO -ov -quiet "$OUT/Weitblick-Remote.dmg"

# Check what users get: mount without showing it in Finder and verify the app inside.
MOUNT="$STAGING/mount"
mkdir "$MOUNT"
hdiutil attach -nobrowse -readonly -quiet -mountpoint "$MOUNT" "$OUT/Weitblick-Remote.dmg"
codesign --verify --strict --deep "$MOUNT/$APP_NAME.app" && status=0 || status=$?
hdiutil detach -quiet "$MOUNT"
[[ $status == 0 ]] || { echo "the app inside the DMG does not verify" >&2; exit 1; }

(cd "$OUT" && ditto -c -k --keepParent "$APP_NAME.app" Weitblick-Remote.zip)
(cd "$OUT" && shasum -a 256 Weitblick-Remote.dmg Weitblick-Remote.zip > SHA256SUMS.txt)

echo "release $VERSION:"
(cd "$OUT" && ls -l Weitblick-Remote.dmg Weitblick-Remote.zip && cat SHA256SUMS.txt)
