#!/usr/bin/env bash
# Archive the built artifacts and write checksums for one release target.
#
# Produces, into OUT (default ./out):
#   - surftp-<TARGET>.tar.gz            (onedir, Unix)     or .zip (Windows)
#   - surftp-<TARGET>-onefile.tar.gz    (onefile, Unix)    or .zip (Windows)
#   - SHA256SUMS                        covering both
#
# Runs from the repository root. Bash works on every GitHub runner (Git Bash on
# Windows), so one script serves all three OSes.
set -euo pipefail

TARGET="${1:?usage: package.sh TARGET [DIST] [OUT]}"
DIST="${2:-dist}"
OUT="${3:-out}"

if [[ ! -d "$DIST/surftp" ]]; then
    echo "error: $DIST/surftp not found — run 'pyinstaller packaging/surftp.spec' first" >&2
    exit 1
fi

mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
DIST="$(cd "$DIST" && pwd)"

cd "$DIST"
if [[ "$(uname -s)" == MINGW* || "$(uname -s)" == MSYS* || "$(uname -s)" == CYGWIN* ]]; then
    SUFFIX=zip
    (cd surftp && zip -qr "$OUT/surftp-$TARGET.zip" .)
    cp surftp-onefile.exe "$OUT/surftp-$TARGET-onefile.zip"
else
    SUFFIX=tar.gz
    tar -czf "$OUT/surftp-$TARGET.tar.gz" surftp
    cp surftp-onefile "$OUT/surftp-$TARGET-onefile.tar.gz"
fi

cd "$OUT"
sha256sum surftp-"$TARGET".$SUFFIX surftp-"$TARGET"-onefile.$SUFFIX > SHA256SUMS
echo "packaged $TARGET:"
ls -lh surftp-"$TARGET".$SUFFIX surftp-"$TARGET"-onefile.$SUFFIX SHA256SUMS