#!/usr/bin/env bash
# Inside-out hardened-runtime codesign of a CloakBrowser Manager .app.
#
# Standalone, used by the GitHub Actions release workflow. This DELIBERATELY
# duplicates the signing block in build_macos.sh rather than sharing code with
# it: build_macos.sh is the proven manual-release path and stays frozen, so a CI
# change here can never destabilise the local fallback. Keep the two in sync by
# hand if the entitlements/flags ever change.
#
# Usage: sign_macos_app.sh "/path/to/CloakBrowser Manager.app"
# Requires env: CB_SIGN_IDENTITY (e.g. "Developer ID Application: Your Name (TEAMID)")
# Optional env: KEYCHAIN (keychain to sign against; codesign uses the search list otherwise)
set -euo pipefail

APP="${1:?usage: sign_macos_app.sh <app>}"
: "${CB_SIGN_IDENTITY:?CB_SIGN_IDENTITY must be set}"
ENT="$(cd "$(dirname "$0")" && pwd)/entitlements.plist"
[ -f "$ENT" ] || { echo "[error] entitlements not found at $ENT"; exit 1; }
KC_ARGS=()
[ -n "${KEYCHAIN:-}" ] && KC_ARGS=(--keychain "$KEYCHAIN")

echo "[sign] $APP  identity=$CB_SIGN_IDENTITY"

# Dylibs / extension modules: hardened runtime, no entitlements (they load into a
# host process whose executable carries the entitlements).
sign_lib() {
  codesign --force --timestamp --options runtime "${KC_ARGS[@]}" \
    --sign "$CB_SIGN_IDENTITY" "$1"
}
# Standalone executables (Playwright's node driver runs as its OWN process, so
# V8's JIT needs the allow-jit entitlements on node's own signature — without
# them node dies "Check failed: 12 == errno" (ENOMEM) at V8 init).
sign_exe() {
  codesign --force --timestamp --options runtime --entitlements "$ENT" "${KC_ARGS[@]}" \
    --sign "$CB_SIGN_IDENTITY" "$1"
}

while IFS= read -r -d '' f; do sign_lib "$f"; done \
  < <(find "$APP/Contents" -type f \( -name "*.so" -o -name "*.dylib" \) -print0)
while IFS= read -r -d '' f; do
  file "$f" | grep -q "Mach-O" && sign_exe "$f" || true
done < <(find "$APP/Contents" -type f -perm +111 ! -name "*.so" ! -name "*.dylib" -print0)

# Finally the app bundle, with entitlements on the main executable.
codesign --force --timestamp --options runtime --entitlements "$ENT" "${KC_ARGS[@]}" \
  --sign "$CB_SIGN_IDENTITY" "$APP"
codesign --verify --strict --verbose=2 "$APP"
echo "[sign] done"
