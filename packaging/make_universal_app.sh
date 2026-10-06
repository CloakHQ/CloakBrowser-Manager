#!/usr/bin/env bash
# Merge a native arm64 .app and a native x86_64 .app into ONE universal .app by
# lipo-joining every shared Mach-O.
#
# PyInstaller's own --target-arch universal2 can't be used here: it needs every
# embedded binary (Playwright's node driver, single-arch wheels) to already be
# universal2, which they are not. So we build each arch natively on its own
# runner and fuse them.
#
# The two arch trees are NOT byte-identical: per-arch wheels legitimately differ
# (e.g. openssl ships an x86_64-only legacy provider dylib on Intel only). So we
# UNION the trees — shared Mach-Os get lipo'd fat; files unique to one arch are
# carried through as-is. The safety gate is: fail if any Mach-O present in BOTH
# trees ends up thin (a real merge failure); a single-arch *extra* that exists on
# only one side is warned about, not fatal (the other arch's native build proved
# it doesn't need it).
#
# Usage: make_universal_app.sh <arm.app> <intel.app> <out.app>
set -euo pipefail

ARM="${1:?usage: make_universal_app.sh <arm.app> <intel.app> <out.app>}"
INTEL="${2:?missing intel .app}"
OUT="${3:?missing output .app}"
for d in "$ARM" "$INTEL"; do
  [ -d "$d" ] || { echo "[error] not a directory: $d"; exit 1; }
done
is_macho() { file -b "$1" 2>/dev/null | grep -q "Mach-O"; }

echo "[universal] arm=$ARM"
echo "[universal] intel=$INTEL"
echo "[universal] out=$OUT"

# 1. Base = a copy of the arm tree (preserves symlinks + perms).
rm -rf "$OUT" 2>/dev/null || true
mkdir -p "$(dirname "$OUT")"
cp -R "$ARM" "$OUT"

# 2. Union in every intel-only path (dirs, symlinks, files).
added=0
while IFS= read -r rel; do
  rel="${rel#./}"; [ -z "$rel" ] && continue
  src="$INTEL/$rel"; dst="$OUT/$rel"
  { [ -e "$dst" ] || [ -L "$dst" ]; } && continue
  if [ -L "$src" ]; then
    mkdir -p "$(dirname "$dst")"; cp -P "$src" "$dst"; added=$((added + 1))
  elif [ -d "$src" ]; then
    mkdir -p "$dst"
  else
    mkdir -p "$(dirname "$dst")"; cp "$src" "$dst"; added=$((added + 1))
  fi
done < <(cd "$INTEL" && find .)
echo "[universal] carried $added intel-only entr(y/ies)"

# 3. Fuse every shared Mach-O.
fused=0; kept_same=0
while IFS= read -r rel; do
  rel="${rel#./}"; [ -z "$rel" ] && continue
  a="$ARM/$rel"; i="$INTEL/$rel"; o="$OUT/$rel"
  [ -L "$a" ] && continue
  [ -f "$a" ] || continue
  { [ -e "$i" ] && [ ! -L "$i" ]; } || continue     # shared regular files only
  is_macho "$a" || continue
  if ! is_macho "$i"; then echo "[warn] Mach-O on arm not intel, keeping arm: $rel"; continue; fi
  if [ "$(lipo -archs "$a" 2>/dev/null)" = "$(lipo -archs "$i" 2>/dev/null)" ]; then
    kept_same=$((kept_same + 1)); continue          # same arch(s) both sides — can't fatten
  fi
  lipo -create "$a" "$i" -output "$o"; fused=$((fused + 1))
done < <(cd "$ARM" && find . -type f)
echo "[universal] fused $fused shared Mach-O(s); kept_same_arch=$kept_same"

# 4. Fatness gate: shared thin = hard error; single-arch extra thin = warn.
shared_thin=0; extra_thin=0
while IFS= read -r -d '' f; do
  is_macho "$f" || continue
  archs="$(lipo -archs "$f" 2>/dev/null || true)"
  case "$archs" in *x86_64*arm64*|*arm64*x86_64*) continue ;; esac
  rel="${f#"$OUT"/}"
  if [ -e "$ARM/$rel" ] && [ -e "$INTEL/$rel" ]; then
    echo "[error] shared Mach-O is thin ($archs): $rel"; shared_thin=$((shared_thin + 1))
  else
    echo "[warn] single-arch extra ($archs): $rel"; extra_thin=$((extra_thin + 1))
  fi
done < <(find "$OUT" -type f ! -type l -print0)
[ "$shared_thin" -eq 0 ] || { echo "[error] $shared_thin shared Mach-O(s) not universal — aborting"; exit 1; }
echo "[universal] OK — all shared binaries are fat; $extra_thin benign single-arch extra(s)"
