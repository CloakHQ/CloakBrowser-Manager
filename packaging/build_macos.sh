#!/usr/bin/env bash
# Build the native macOS CloakBrowser Manager: freeze -> .app -> .dmg,
# optionally codesign + notarize + staple.
#
# Runs unsigned locally when no signing env is set. CI-ready: the same script
# signs/notarizes when these env vars are provided:
#   CB_SIGN_IDENTITY   "Developer ID Application: Your Name (TEAMID)"
#   CB_NOTARY_PROFILE  notarytool keychain profile name
#     (create once: xcrun notarytool store-credentials CB_NOTARY_PROFILE \
#        --apple-id you@example.com --team-id TEAMID --password app-specific-pw)
#
# Output: dist_native/CloakBrowser-Manager-<version>.dmg
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

APP_NAME="CloakBrowser Manager"
VERSION="$(git describe --tags --always 2>/dev/null || echo dev)"
DIST="$ROOT/dist_native"
BUILD="$ROOT/build_native"
BUILD_VENV="${PACKAGING_VENV:-$ROOT/.venv-build}"

echo "[build] CloakBrowser Manager $VERSION (macOS)"

# Bake the version into the bundle so the frozen app can log its own build
# (no .git at runtime). manager.spec ships backend/version.txt as data.
printf '%s' "$VERSION" > "$ROOT/backend/version.txt"

# 1. Build the React frontend (embedded into the app as data).
echo "[build] frontend"
( cd frontend && npm ci && npm run build )

# 2. Clean-room build venv with runtime + build deps.
# PYTHON overrides the interpreter (e.g. /opt/homebrew/bin/python3.14 on a box
# whose system python3 is too old, like the Apple Silicon build machine).
PYTHON="${PYTHON:-python3}"
if [ ! -x "$BUILD_VENV/bin/python" ]; then
  echo "[build] creating build venv at $BUILD_VENV ($PYTHON)"
  "$PYTHON" -m venv "$BUILD_VENV"
fi
"$BUILD_VENV/bin/python" -m pip install --disable-pip-version-check -q \
  -r backend/requirements.txt -r packaging/requirements-build.txt

# 2a. Pin cryptography to the self-contained universal2 wheel (static OpenSSL).
# The newest cryptography ships no single-arch macOS wheel, so on the Intel
# runner pip source-builds it against Homebrew's openssl@3 and PyInstaller then
# bundles that libssl.3.dylib — which mismatches the extension and makes the
# frozen app die at launch: `dlopen(_rust.abi3.so): Symbol not found:
# _SSL_get0_group_name`. The universal2 wheel statically links OpenSSL (no
# external libssl) and is fat, so BOTH arches get an identical static extension
# and the later lipo merge is a no-op. --force-reinstall replaces whatever the
# requirements step resolved; --only-binary forbids a source build.
"$BUILD_VENV/bin/python" -m pip install --disable-pip-version-check -q \
  --force-reinstall --no-deps --only-binary=:all: 'cryptography>=44,<49'

# 3. Freeze.
echo "[build] pyinstaller"
rm -rf "$DIST" "$BUILD"
"$BUILD_VENV/bin/pyinstaller" --noconfirm --clean \
  --distpath "$DIST" --workpath "$BUILD" \
  packaging/manager.spec

APP="$DIST/$APP_NAME.app"
[ -d "$APP" ] || { echo "[error] $APP not produced"; exit 1; }

# 3a. Guard: the frozen cryptography extension must NOT link an external libssl.
# That dependency is the launch-crash bug (a dynamically-linked, source-built
# cryptography leaking Homebrew's OpenSSL into the bundle). Step 2a prevents it;
# fail loud here if it ever regresses instead of shipping a broken .dmg.
RUST_SO="$(find "$APP/Contents" -path '*cryptography*' -name '_rust.abi3.so' | head -1)"
if [ -n "$RUST_SO" ] && otool -L "$RUST_SO" | grep -q 'libssl'; then
  echo "[error] cryptography _rust.abi3.so links external libssl — dynamic OpenSSL leaked into the bundle:"
  otool -L "$RUST_SO" | grep -iE 'ssl|crypto'
  exit 1
fi

# 4. Codesign (hardened runtime) if an identity is provided.
#    Sign inside-out: every nested Mach-O first, then the bundle. `--deep` is
#    unreliable with `--timestamp` (fails "a timestamp was expected but was not
#    found" on a subcomponent), so we sign components explicitly.
if [ -n "${CB_SIGN_IDENTITY:-}" ]; then
  echo "[build] codesign (inside-out): $CB_SIGN_IDENTITY"
  ENT="packaging/entitlements.plist"
  # Dylibs / extension modules: hardened runtime, no entitlements (they load
  # into a host process whose executable carries the entitlements).
  sign_lib() {
    codesign --force --timestamp --options runtime --sign "$CB_SIGN_IDENTITY" "$1"
  }
  # Standalone executables (Playwright's node driver runs as its OWN process,
  # so V8's JIT needs the allow-jit entitlements on node's own signature —
  # without them node dies "Check failed: 12 == errno" (ENOMEM) at V8 init).
  sign_exe() {
    codesign --force --timestamp --options runtime --entitlements "$ENT" \
      --sign "$CB_SIGN_IDENTITY" "$1"
  }
  while IFS= read -r -d '' f; do sign_lib "$f"; done \
    < <(find "$APP/Contents" -type f \( -name "*.so" -o -name "*.dylib" \) -print0)
  while IFS= read -r -d '' f; do
    file "$f" | grep -q "Mach-O" && sign_exe "$f" || true
  done < <(find "$APP/Contents" -type f -perm +111 ! -name "*.so" ! -name "*.dylib" -print0)
  # Finally the app bundle, with entitlements on the main executable.
  codesign --force --timestamp --options runtime \
    --entitlements packaging/entitlements.plist \
    --sign "$CB_SIGN_IDENTITY" "$APP"
  codesign --verify --strict --verbose=2 "$APP"
else
  echo "[build] no CB_SIGN_IDENTITY set — leaving app unsigned"
fi

# 5. Build the .dmg (drag-to-Applications layout).
# Detach any stale "$APP_NAME" volume first — an already-mounted copy (e.g. a
# previously-opened dmg) collides with the new volume name and fails hdiutil
# with "Operation not permitted".
for v in "/Volumes/$APP_NAME"*; do
  [ -d "$v" ] && { hdiutil detach "$v" -force >/dev/null 2>&1 || diskutil unmount force "$v" >/dev/null 2>&1 || true; }
done
DMG="$DIST/CloakBrowser-Manager-$VERSION.dmg"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
echo "[build] dmg -> $DMG"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"

# 6. Notarize + staple if signed and a notary profile is configured.
if [ -n "${CB_SIGN_IDENTITY:-}" ] && [ -n "${CB_NOTARY_PROFILE:-}" ]; then
  echo "[build] notarize (waits for Apple)"
  xcrun notarytool submit "$DMG" --keychain-profile "$CB_NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG"
  xcrun stapler staple "$APP"
  echo "[build] notarized + stapled"
else
  echo "[build] skipping notarization (need CB_SIGN_IDENTITY + CB_NOTARY_PROFILE)"
fi

echo "[done] $DMG"
