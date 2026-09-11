# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for SURFTP.

Builds **one** artifact per invocation, selected by the ``SURFTP_BUILD_MODE``
environment variable:

    SURFTP_BUILD_MODE=onedir  pyinstaller packaging/surftp.spec   # dist/surftp/
    SURFTP_BUILD_MODE=onefile pyinstaller packaging/surftp.spec   # dist/surftp-onefile

Why not both in one pass: an earlier version of this spec emitted a onefile
``EXE`` and a onedir ``COLLECT`` from a single ``Analysis``. The onedir
artifact was fine; the onefile one built without error and then failed at
startup with ``Failed to extract entry: libexpat.so.1`` — the two passes share
one ``Analysis`` and one workpath, and the embedded PKG came out corrupt. A
build that reports success and ships a binary that cannot start is the exact
failure this packaging work exists to prevent, so the modes are now separate.
CI runs the spec twice with separate ``--workpath`` values.

All data paths are anchored to SPECPATH (the directory holding this file), not
the working directory, so the build is identical locally and in CI. Keep the
excludes: a transitive pull-in should not silently add tens of MB.
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

ROOT = Path(SPECPATH).parent

MODE = os.environ.get("SURFTP_BUILD_MODE", "onedir").strip().lower()
if MODE not in {"onedir", "onefile"}:
    raise SystemExit(f"SURFTP_BUILD_MODE must be 'onedir' or 'onefile', got {MODE!r}")

datas = [(str(ROOT / "surftp" / "app.tcss"), "surftp")]
# aioftp reads its own distribution metadata at import time; without this the
# frozen app dies with `PackageNotFoundError` at startup.
datas += copy_metadata("aioftp")
# Textual lazy-loads widgets through __getattr__ + import_module, which static
# analysis cannot see (SessionTabs is exactly such a lazily-imported part).
datas += collect_data_files("textual")
hiddenimports = collect_submodules("textual")

excludes = [
    "textual_dev",
    "textual_serve",
    "aiohttp",
    "pytest",
    "tkinter",
    "test",
]

# strip is free space on ELF/Mach-O; leave Windows binaries unstripped (PE
# files have no .symtab to drop). Never UPX: it is the most common trigger for
# antivirus false positives on Windows, and a client Defender quarantines is
# worse than one that is 40 MB larger.
strip = sys.platform != "win32"

a = Analysis(
    [str(ROOT / "surftp" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

if MODE == "onefile":
    # Single-file convenience build: one file to drop on a server, at the cost
    # of a ~1 s re-extraction on every launch. `freeze_support()` in the entry
    # point lets the multiprocessing-spawned emulator child reuse the parent's
    # extraction, so onefile + spawn works.
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="surftp-onefile",
        debug=False,
        bootloader_ignore_signals=False,
        strip=strip,
        upx=False,
        console=True,
    )
else:
    # The primary artifact: ~0.45 s startup instead of ~1.5 s, the shape macOS
    # signing and notarisation need, and the one least likely to trip Windows
    # heuristics.
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="surftp",
        debug=False,
        bootloader_ignore_signals=False,
        strip=strip,
        upx=False,
        console=True,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=strip,
        upx=False,
        name="surftp",
    )
