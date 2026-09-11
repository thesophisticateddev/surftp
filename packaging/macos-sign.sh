#!/usr/bin/env bash
# Codesign and notarise a macOS artifact.
#
# Unsigned, a downloaded binary is refused by Gatekeeper with "cannot be opened
# because the developer cannot be verified", and the workaround (right-click →
# Open, or stripping the quarantine attribute) is a support burden on every
# single user. So signing is not optional for an artifact people download.
#
# It *is* optional for the build to proceed: forks and pull requests have no
# secrets, and must still produce a testable binary. With no credentials in the
# environment this script prints why and exits 0, leaving the artifact unsigned.
#
# Usage: packaging/macos-sign.sh <path-to-binary-or-bundle-dir>
#
# Environment (all required together, all from GitHub Secrets):
#   MACOS_CERTIFICATE        base64 of the Developer ID Application .p12
#   MACOS_CERTIFICATE_PWD    password for that .p12
#   MACOS_SIGN_IDENTITY      e.g. "Developer ID Application: Name (TEAMID)"
#   MACOS_NOTARY_APPLE_ID    Apple ID used for notarytool
#   MACOS_NOTARY_PASSWORD    app-specific password for that Apple ID
#   MACOS_NOTARY_TEAM_ID     team ID for notarytool
#
# Secret discipline: `set +x` is never lifted in this file, no secret is ever
# echoed, and the temporary keychain is deleted on exit even on failure.

set -euo pipefail
set +x  # never trace: the environment below holds a certificate password

TARGET="${1:?usage: macos-sign.sh <path>}"
ENTITLEMENTS="$(cd "$(dirname "$0")" && pwd)/entitlements.plist"

if [[ -z "${MACOS_CERTIFICATE:-}" || -z "${MACOS_SIGN_IDENTITY:-}" ]]; then
    echo "macos-sign: no signing secrets in the environment - leaving '$TARGET' unsigned."
    echo "macos-sign: the artifact is still testable locally; it will trip Gatekeeper if downloaded."
    exit 0
fi

KEYCHAIN="$RUNNER_TEMP/surftp-signing.keychain-db"
KEYCHAIN_PWD="$(uuidgen)"   # ephemeral, dies with the runner

cleanup() {
    security delete-keychain "$KEYCHAIN" 2>/dev/null || true
    rm -f "$RUNNER_TEMP/certificate.p12"
}
trap cleanup EXIT

echo "macos-sign: importing certificate into an ephemeral keychain"
echo -n "$MACOS_CERTIFICATE" | base64 --decode > "$RUNNER_TEMP/certificate.p12"
security create-keychain -p "$KEYCHAIN_PWD" "$KEYCHAIN"
security set-keychain-settings -lut 21600 "$KEYCHAIN"
security unlock-keychain -p "$KEYCHAIN_PWD" "$KEYCHAIN"
security import "$RUNNER_TEMP/certificate.p12" -k "$KEYCHAIN" \
    -P "$MACOS_CERTIFICATE_PWD" -T /usr/bin/codesign
security set-key-partition-list -S apple-tool:,apple:,codesign: \
    -s -k "$KEYCHAIN_PWD" "$KEYCHAIN" > /dev/null
security list-keychain -d user -s "$KEYCHAIN" login.keychain-db

# Sign inside-out: every nested Mach-O first, the top-level executable last.
# Signing the outer binary first invalidates as soon as a nested .dylib is
# re-signed, and the hardened runtime rejects the result at launch.
if [[ -d "$TARGET" ]]; then
    echo "macos-sign: signing nested libraries"
    find "$TARGET" -type f \( -name "*.dylib" -o -name "*.so" \) -print0 |
        while IFS= read -r -d '' lib; do
            codesign --force --timestamp --options runtime \
                --sign "$MACOS_SIGN_IDENTITY" "$lib"
        done
    MAIN="$TARGET/$(basename "$TARGET")"
else
    MAIN="$TARGET"
fi

echo "macos-sign: signing $MAIN"
codesign --force --timestamp --options runtime \
    --entitlements "$ENTITLEMENTS" \
    --sign "$MACOS_SIGN_IDENTITY" "$MAIN"
codesign --verify --deep --strict --verbose=2 "$MAIN"

if [[ -z "${MACOS_NOTARY_APPLE_ID:-}" ]]; then
    echo "macos-sign: signed but not notarised (no notary credentials)."
    exit 0
fi

# Notarisation takes a zip, not a loose binary or directory.
ZIP="$RUNNER_TEMP/notarize-$(basename "$TARGET").zip"
ditto -c -k --keepParent "$TARGET" "$ZIP"

echo "macos-sign: submitting for notarisation (this waits for Apple)"
xcrun notarytool submit "$ZIP" \
    --apple-id "$MACOS_NOTARY_APPLE_ID" \
    --password "$MACOS_NOTARY_PASSWORD" \
    --team-id "$MACOS_NOTARY_TEAM_ID" \
    --wait --timeout 30m

# Deliberately no `stapler staple`: a ticket can only be stapled to an .app,
# .dmg or .pkg, and this is a bare command-line binary. Gatekeeper checks
# notarisation online for these, which is why the submission above is what
# matters. `xcrun stapler staple` here would fail with error 73.
rm -f "$ZIP"
echo "macos-sign: done - $MAIN is signed and notarised"
