#!/usr/bin/env python3
"""Drive the built artifacts from outside: the packaging suite.

Everything here runs the *binary* as a user would — no imports from ``surftp``,
because the point is to exercise a bundle built from it. The in-bundle checks
live in ``surftp/selfcheck.py``; this script is what invokes them, plus the two
assertions only visible from outside: that the process terminates instead of
hanging, and that it leaves no stray processes behind.

Usage (after ``pyinstaller packaging/surftp.spec``):

    python tests/test_frozen.py [dist-directory]
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

EXE_SUFFIX = ".exe" if os.name == "nt" else ""
# Generous: a onefile bundle re-extracts ~40 MB before it runs a line of code,
# and a cold CI runner is slower still. A hang is what this bounds, not slowness.
TIMEOUT_SECONDS = 300

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    """Print one PASS/FAIL line and record the failure."""
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}{f': {detail}' if detail else ''}")
    if not ok:
        failures += 1


def expected_version() -> str:
    """Read the version from ``__about__.py`` without importing the package.

    Importing would pull in the whole dependency tree; this suite deliberately
    depends on nothing but the built artifact.
    """
    about = Path(__file__).resolve().parent.parent / "surftp" / "__about__.py"
    for line in about.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("could not read __version__ from surftp/__about__.py")


def run(binary: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the artifact with a hard timeout, capturing both streams."""
    binary = binary.resolve()  # absolute: cwd is the artifact's own directory
    return subprocess.run(
        [str(binary), *args],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        # Keep the artifact's own working directory out of the picture: the
        # binary must not depend on being launched from the source tree.
        cwd=str(Path(binary).resolve().parent),
    )


def strays(binary: Path) -> int:
    """Count processes still matching the binary name. POSIX only; -1 elsewhere.

    The fork-bomb symptom was strays *surviving the parent*, which no in-bundle
    check can see: by the time it could look, it is one of them.

    Matches the artifact's **absolute path**, not its name: this suite lives in
    a directory called ``surftp`` too, so a name match would count itself.
    """
    if os.name == "nt" or shutil.which("pgrep") is None:
        return -1
    result = subprocess.run(
        ["pgrep", "-f", str(binary.resolve())], capture_output=True, text=True
    )
    return len([line for line in result.stdout.split() if line.strip()])


def verify(binary: Path, label: str) -> None:
    """Run every external assertion against one artifact."""
    print(f"════ {label}: {binary}")
    if not binary.exists():
        check(f"{label} exists", False, "artifact not found")
        return
    check(f"{label} exists", True, f"{binary.stat().st_size / 1_000_000:.0f} MB")

    # --version exercises import of the entry point and its metadata reads:
    # this is the check that catches a missing .dist-info.
    try:
        result = run(binary, "--version")
    except subprocess.TimeoutExpired:
        check(f"{label} --version", False, f"timed out after {TIMEOUT_SECONDS}s")
        return
    version = expected_version()
    ok = result.returncode == 0 and version in result.stdout
    check(f"{label} --version", ok, (result.stdout + result.stderr).strip()[:400])

    try:
        result = run(binary, "--self-check")
    except subprocess.TimeoutExpired:
        check(f"{label} --self-check", False, f"timed out after {TIMEOUT_SECONDS}s — likely a hang or a spawn loop")
        return
    for line in result.stdout.splitlines():
        print(f"      │ {line}")
    check(f"{label} --self-check", result.returncode == 0, result.stderr.strip()[:400])

    remaining = strays(binary)
    if remaining < 0:
        print(f"      │ stray-process check skipped (no pgrep on {sys.platform})")
    else:
        check(f"{label} no strays", remaining == 0, f"{remaining} process(es) still running")


def main() -> int:
    """Verify every artifact found in the dist directory."""
    root = Path(__file__).resolve().parent.parent
    dist = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "dist"
    onedir = dist / "surftp" / f"surftp{EXE_SUFFIX}"
    onefile = dist / f"surftp-onefile{EXE_SUFFIX}"

    if not onedir.exists() and not onefile.exists():
        print(f"FAIL  no artifacts in {dist} — run: pyinstaller packaging/surftp.spec")
        return 1

    verify(onedir, "onedir")
    verify(onefile, "onefile")

    print()
    print("FROZEN SUITE PASSED" if not failures else f"FROZEN SUITE FAILED ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
