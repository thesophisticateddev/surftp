#!/usr/bin/env bash
# Codesign and notarise the macOS build.
#
# Skips gracefully (exit 0) when the signing secrets are absent, so forks and
# PR-triggered builds still succeed unsigned. Requires the onedir bundle at
# DIST/surftp/surftp; uses the hardened-runtime entitlements in this directory
# (a frozen CPython needs allow-jit / unsigned-executable-memory / library
# validation disabled or the cffi/R extensions crash on first launch).
#
# Required env, injected by the release workflow from GitHub Secrets:
#   MACOS_CERTIFICATE, MACOS_CERTIFICATE_PWD,
#   MACOS_NOTARIZATION_APPLE_ID, MACOS_NOTARIZATION_TEAM_ID,
#   MACOS_NOTARIZATION_PASSWORD
set -euo pipefail

DIST="${1:-dist}"
BIN="$DIST/surftp/surftp"
ENTITLEMENTS="$(cd "$(dirname "$0")" && pwd)/entitlements.plist"

if [[ -z "${MACOS_CERTIFICATE:-}" ]]; then
    echo "Skipping macOS codesign/notarisation (no MACOS_CERTIFICATE secret)."
    exit 0
fi
if [[ ! -x "$BIN" ]]; then
    echo "error: $BIN not found" >&2
    exit 1
fi

# --- import the Developer ID cert into a throwaway keychain -----------------
KEYCHAIN=build.keychain-db
security create-keychain -p ci "$KEYCHAIN"
security set-keychain-settings -lut 300 "$KEYCHAIN"
# Import from a base64 env var without ever echoing the certificate.
echo "$MACOS_CERTIFICATE" | base64 --decode > /tmp/cert.p12
security import /tmp/cert.p12 -k "$KEYCHAIN" \
    -P "$MACOS_CERTIFICATE_PWD" -T /usr/bin/codesign
rm -f /tmp/cert.p12
security list-keychains -d user -s "$KEYCHAIN"
security unlock-keychain -p ci "$KEYCHAIN"
security set-key-partition-list -S apple-tool:,apple: -s -k ci "$KEYCHAIN" >/dev/null

# --- codesign the binary (hardened runtime + entitlements) ------------------
IDENTITY=$(security find-identity -v -p codesigning "$KEYCHAIN" \
    | awk '/Developer ID Application/{print $2; exit}')
echo "Signing with identity: $IDENTITY"
codesign --force --options runtime --entitlements "$ENTITLEMENTS" \
    --sign "$IDENTITY" --timestamp "$BIN"

# --- notarise and staple -----------------------------------------------------
ZIP=/tmp/surftp-notarize.zip
(cd "$DIST" && zip -qr "$ZIP" surftp)
xcrun notarytool submit "$ZIP" \
    --apple-id "$MACOS_NOTARIZATION_APPLE_ID" \
    --team-id "$MACOS_NOTARIZATION_TEAM_ID" \
    --password "$MACOS_NOTARIZATION_PASSWORD" \
    --wait
xcrun stapler staple "$BIN"
codesign --verify --verbose "$BIN"
echo "macOS signing + notarisation complete."

security delete-keychain "$KEYCHAIN"