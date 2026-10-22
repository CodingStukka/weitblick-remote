# Building, testing and releasing

## Prerequisites

- macOS 14 or later on Apple Silicon, Xcode 26, [XcodeGen](https://github.com/yonaskolb/XcodeGen)
- Homebrew `openssl@3`, `cmake` and `ninja` (FreeRDP and OpenSSL are linked statically)
- `git submodule update --init` (FreeRDP 3.32.1 in `vendor/FreeRDP`)

## Build

    scripts/build.sh [Debug|Release]

Builds FreeRDP into `vendor/install` when needed, generates `WeitblickRemote.xcodeproj` from `project.yml`
and puts `build/Weitblick Remote.app`, `build/weitblick-smoke` and `build/weitblick-e2e` next to each other.
Open the generated project in Xcode for day-to-day work; regenerate it with `xcodegen generate` after
changing `project.yml`.

## Signing

Builds are signed ad-hoc by default (`Config/Signing.xcconfig`), so a fresh clone needs no Apple account.
macOS ties privacy grants (Accessibility for the system shortcuts, microphone) and Keychain access to the
signature, so ad-hoc builds ask again after every update. To keep them, create a gitignored
`Local.xcconfig` in the repository root with your own identity:

    CODE_SIGN_IDENTITY = Apple Development
    DEVELOPMENT_TEAM = <your team id>

Xcode builds and `scripts/build.sh` pick it up automatically.

## Test

    (cd Packages/KeyboardEngine && swift test)
    (cd Packages/ConnectionStore && swift test)
    xcodebuild -project WeitblickRemote.xcodeproj -scheme WeitblickRemote -derivedDataPath build/DerivedData test
    scripts/smoke.sh                 # headless connect, resize, end reasons
    scripts/e2e.sh                   # keyboard, clipboard, drives, audio, printers, reconnect (3 runs)

`smoke.sh` and `e2e.sh` connect to a Windows test VM described in a gitignored `.testvm.env`
(`WEITBLICK_TEST_HOST`, `WEITBLICK_TEST_USER`, `WEITBLICK_TEST_PASS`) and drive it with Parallels' `prlctl`.
They serialize on `/tmp/weitblick-testvm.lock`; builds serialize on `/tmp/weitblick-build.lock`. Do not build
while `e2e.sh` runs: the VM shares the Mac's CPU and typing checks get flaky under load.

## Release

1. Set `MARKETING_VERSION` (and `CURRENT_PROJECT_VERSION`) in `project.yml`, the same version as `VERSION` in
   `linux/weitblick/__init__.py`, and add it with the date as the first `<release>` in
   `linux/data/nrw.neuhaus.WeitblickRemote.metainfo.xml` (the Linux tests check that they match). When FreeRDP or
   OpenSSL change, update their versions in `THIRD_PARTY_NOTICES.md`; a new `vendor/FreeRDP` commit goes into
   `linux/flatpak/nrw.neuhaus.WeitblickRemote.yml` too (checked by the Linux tests as well).
2. `scripts/release.sh <version>` builds the Release configuration unsigned, signs the app ad-hoc with the
   hardened runtime, checks it with `codesign --verify --strict`, and writes to `build/release/`:
   - `Weitblick-Remote.dmg` (the app and a link to Applications; the script mounts it and verifies the app inside)
   - `Weitblick-Remote.zip`
   - `SHA256SUMS.txt`
3. Tag the commit `v<version>` and attach the three files to a GitHub release. Keep the file names as they are:
   the website links to `releases/latest/download/Weitblick-Remote.dmg`.
4. Publishing the release starts the Linux workflow (`.github/workflows/linux.yml`), which builds and tests the
   Linux version and attaches `Weitblick-Remote-amd64.deb`, `Weitblick-Remote-arm64.deb`,
   `Weitblick-Remote-x86_64.flatpak`, `Weitblick-Remote-aarch64.flatpak` and `SHA256SUMS-Linux.txt`. It fails if
   the package version doesn't match the tag.

The app is not notarized, so Gatekeeper rejects it (`spctl -a -vv` says "rejected"). Users allow it once:
on macOS 15 and later, try to open it, then System Settings → Privacy & Security → Open Anyway; on macOS 14,
right-click → Open. Removing the quarantine flag works everywhere:
`xattr -dr com.apple.quarantine "/Applications/Weitblick Remote.app"`.

## Install a personal build

    scripts/install-local.sh

Builds the Release configuration signed with the identity from `Local.xcconfig` and installs it as
`/Applications/Weitblick Remote.app` (it refuses while the app is running). It does not launch the app.

## App icon

The icon sources are SVGs in `design/icon` (`app-icon.svg`, plus pixel-grid variants for 16 and 32 px).
`scripts/render-icon.sh` (needs `rsvg-convert` and ImageMagick) renders the asset catalog and the website
exports in `design/icon/export`; the rendered PNGs are committed. The Linux version takes its icons from there
too (`make -C linux` copies them into the hicolor sizes).
