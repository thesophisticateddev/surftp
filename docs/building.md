# Building SURFTP as a standalone binary

SURFTP ships as a self-contained executable: a bundled CPython 3.12, the ten runtime dependencies,
and the app. Users need no Python, no virtualenv and no `pip`. This page covers building one
yourself — because you want a patched build, a platform CI does not cover, or simply to check that
the published binary is what the source produces.

Releases are built by [`.github/workflows/release.yml`](../.github/workflows/release.yml) on every
`v*` tag. The steps below are the same ones that workflow runs, so a local build and a CI build
produce the same thing.

## What you get

Each platform produces two artifacts from the same source:

| Artifact | Size | Startup | Use it when |
| --- | --- | --- | --- |
| `dist/surftp/` (onedir) | ~100 MB | ~0.45 s | **the default.** A directory; run `./surftp` inside it |
| `dist/surftp-onefile` | ~42 MB | ~1.5 s | you want one file to `scp` onto a server |

The onefile build unpacks itself into a temporary directory on **every** launch, which is where its
extra second goes. It is a convenience format, not a faster one.

> On size: 57% of the bundle is DuckDB — a full analytical database engine backing a vault that holds
> a handful of connection profiles. That is the lever if the size matters to you, not compression
> flags. See §3 of [`.plans/plan-packaging.md`](../.plans/plan-packaging.md).

## Prerequisites

- **Python 3.12**, matching the version you want bundled. PyInstaller freezes the interpreter you
  build with; it cannot cross-build for another Python.
- The build tooling from `requirements-dev.txt` (which includes PyInstaller and pulls in
  `requirements.txt`).
- **You can only build for the platform you are on.** There is no cross-compilation: a Windows `.exe`
  needs a Windows machine, an arm64 macOS binary needs an arm64 Mac. This is why the release workflow
  has a four-way matrix rather than one job.

```bash
python3.12 -m venv tenv
source tenv/bin/activate
pip install -r requirements-dev.txt
```

## Build it

Two invocations, one per artifact. **Use separate `--workpath` values** — see the warning below.

```bash
# The primary artifact -> dist/surftp/
SURFTP_BUILD_MODE=onedir  pyinstaller --noconfirm --clean --workpath build/onedir packaging/surftp.spec

# The single-file build -> dist/surftp-onefile
SURFTP_BUILD_MODE=onefile pyinstaller --noconfirm --workpath build/onefile packaging/surftp.spec
```

On Windows, set the variable the PowerShell way:

```powershell
$env:SURFTP_BUILD_MODE = "onedir"
pyinstaller --noconfirm --clean --workpath build/onedir packaging/surftp.spec
```

> **Do not try to build both modes in one pass.** An earlier version of the spec emitted a onefile
> `EXE` and a onedir `COLLECT` from a single `Analysis`. It built with no error and produced a
> onefile binary that died at startup with `Failed to extract entry: libexpat.so.1` — the shared
> `Analysis` and workpath corrupted the embedded archive. The spec now refuses to do this; the
> environment variable is how you pick.

## Verify it — this is not optional

Every packaging bug this project has hit built cleanly and failed at runtime: missing `aioftp`
metadata, a lazily-imported Textual widget, an absent `app.tcss`, and a `multiprocessing` fork bomb
that left 23 stray processes after a single run. **A successful build tells you nothing.** So run:

```bash
python tests/test_frozen.py
```

It drives both artifacts as a user would and prints one PASS/FAIL line per check:

- `--version` — exercises the entry point and its metadata reads, catching a missing `.dist-info`.
- `--self-check` — a hidden flag that runs [`surftp/selfcheck.py`](../surftp/selfcheck.py) *inside*
  the bundle: every transport and store module imports, the app mounts headlessly with its stylesheet
  and both panes, the emulator child spawns and returns a frame with no strays, and the vault
  resolves to your user data directory rather than into the bundle.
- A stray-process count after the binary exits, which is the `freeze_support()` regression guard.

You can run the same self-check against a source checkout:

```bash
python -m surftp --self-check
```

If the frozen suite passes, launch the real thing once — a TUI has failure modes a headless test
cannot see:

```bash
./dist/surftp/surftp
```

## Per-platform notes

### Linux

PyInstaller links against the **build machine's glibc**, so a binary built on Ubuntu 24.04 will not
start on 22.04. Build on the oldest distribution you intend to support; CI uses `ubuntu-22.04` for
exactly this reason.

Distribute as `.tar.gz`, never `.zip`: zip does not preserve the executable bit.

### macOS

Build separately for each architecture — `macos-13` for x86_64, `macos-14` for arm64. A `universal2`
binary would need universal wheels for *every* native dependency, and DuckDB and `cryptography` ship
per-architecture wheels only.

An unsigned binary that someone **downloads** is refused by Gatekeeper ("cannot be opened because the
developer cannot be verified"). A binary you built locally is fine — the quarantine attribute comes
from the download, not the build. To sign and notarise:

```bash
export MACOS_CERTIFICATE=...        # base64 of a Developer ID Application .p12
export MACOS_CERTIFICATE_PWD=...
export MACOS_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
export MACOS_NOTARY_APPLE_ID=...    # Apple ID
export MACOS_NOTARY_PASSWORD=...    # app-specific password
export MACOS_NOTARY_TEAM_ID=...
packaging/macos-sign.sh dist/surftp
```

With no credentials in the environment the script says so and exits 0, leaving the binary unsigned —
so forks and pull requests still get something testable. It signs nested `.dylib`/`.so` files before
the main executable (signing outside-in invalidates the outer signature), applies
[`packaging/entitlements.plist`](../packaging/entitlements.plist), and notarises. It deliberately does
**not** staple: a notarisation ticket can only be stapled to an `.app`, `.dmg` or `.pkg`, and this is
a bare command-line binary — Gatekeeper checks those online.

The entitlements exist because the hardened runtime that notarisation requires otherwise breaks a
frozen CPython: it needs writable-executable memory for cffi trampolines, and the bundled `.so` files
are signed by you rather than by Apple.

### Windows

Build with `windows-latest`/any Windows machine; the spec already targets the console subsystem, which
a TUI requires.

Expect **SmartScreen and antivirus friction** on an unsigned PyInstaller binary. Prefer the onedir zip
for distribution: a self-extracting single file is the shape heuristics flag hardest. The spec never
uses UPX for the same reason — compression is the single most common cause of false positives, and a
client Defender quarantines is worse than one that is 40 MB larger.

Run SURFTP in **Windows Terminal**. The legacy `conhost.exe` renders Textual poorly.

## Where the user's data goes

The frozen binary is stateless. The vault lives in `platformdirs.user_data_dir("surftp")` and host
keys in `~/.ssh/known_hosts`, both resolved at runtime and unaffected by freezing. The
`vault` check in the self-check asserts this explicitly: a onefile bundle's extraction directory is
deleted on exit, so a vault written beside the executable would silently lose every saved credential.

## Installing from source instead

If no published binary fits your platform, `pyproject.toml` makes the normal path work:

```bash
pip install .
surftp
```

This needs Python 3.12+ on the target machine — which is exactly what the standalone binaries exist to
avoid.
