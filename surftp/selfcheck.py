"""Runtime self-verification of a packaged build, run from inside the bundle.

Why this ships with the application rather than living in ``tests/``: every
packaging failure recorded in ``.plans/plan-packaging.md`` — the missing
``aioftp`` metadata, the lazily-imported Textual widget, the absent
``app.tcss``, the ``spawn`` process bomb — was a *runtime* failure that the
build itself reported as success, and none of them reproduce in a source
checkout. Checking them therefore requires code executing inside the frozen
bundle, with its own ``sys._MEIPASS``, its own metadata and its own
re-executing ``spawn``. A test file outside the bundle cannot get there.

It is reached through the hidden ``--self-check`` flag, costs a few kB, and is
the release workflow's gate: an artifact that fails it is never uploaded.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import sys
from pathlib import Path

Check = tuple[str, bool, str]


def _bundle_root() -> Path:
    """Return the directory the running program lives in.

    For a frozen build that is the bundle directory (onedir) or the temporary
    extraction root (onefile); for a source checkout it is wherever the
    interpreter sits. Used only to assert that user data does *not* land here.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(sys.prefix).resolve()


async def _check_ui() -> Check:
    """Mount the app headlessly and assert the stylesheet and both panes loaded.

    Catches the two failures a build reports as success: ``app.tcss`` missing
    from ``datas`` (the app runs unstyled), and a Textual widget that is
    lazy-imported and so invisible to PyInstaller's static analysis — the
    session tab machinery is exactly such a part, and it is what mounts the
    panes below.
    """
    from surftp.app import SurfFTPApp
    from surftp.widgets.pane import FilePane

    app = SurfFTPApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        sources = getattr(app.stylesheet, "source", {})
        if not any("app.tcss" in str(key) for key in sources):
            return ("ui.css", False, "app.tcss is not in the loaded stylesheet")
        if not app.stylesheet.rules:
            return ("ui.css", False, "stylesheet loaded but parsed to zero rules")
        for side, pane in (("left", app.left_pane), ("right", app.right_pane)):
            if not isinstance(pane, FilePane):
                return ("ui.panes", False, f"{side} pane is {type(pane).__name__}")
    return ("ui", True, f"{len(app.stylesheet.rules)} CSS rules, both panes mounted")


def _check_spawn() -> Check:
    """Spawn the real emulator child and assert one child starts and none leak.

    This is the ``freeze_support()`` regression guard. Under PyInstaller the
    ``spawn`` context re-executes *the bundle*; without
    ``multiprocessing.freeze_support()`` as the first statement of the entry
    point, the child re-runs ``main()`` and spawns again — a probe of this
    exact pattern left 23 stray processes behind after one run. The real
    emulator target is used rather than a stub, because the target is what the
    child re-imports.
    """
    from surftp.shell import emulator
    from surftp.shell.protocol import FEED, FRAME, QUIT, RESIZE, MessageReader, frame

    before = len(multiprocessing.active_children())
    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=True)
    process = ctx.Process(target=emulator.main, args=(child_conn,), daemon=True)
    process.start()
    child_conn.close()
    try:
        started = len(multiprocessing.active_children()) - before
        if started != 1:
            return ("spawn", False, f"expected 1 child process, saw {started}")

        parent_conn.send_bytes(frame({"t": RESIZE, "cols": 80, "rows": 24}))
        parent_conn.send_bytes(frame({"t": FEED, "d": b"surftp self-check\r\n"}))
        reader = MessageReader()
        deadline = 10.0
        got_frame = False
        while deadline > 0 and not got_frame:
            if not parent_conn.poll(0.5):
                deadline -= 0.5
                continue
            for message in reader.feed(parent_conn.recv_bytes()):
                if message.get("t") == FRAME:
                    got_frame = True
        if not got_frame:
            return ("spawn", False, "emulator child produced no frame within 10 s")
        parent_conn.send_bytes(frame({"t": QUIT}))
    finally:
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        parent_conn.close()

    leaked = len(multiprocessing.active_children()) - before
    if leaked != 0:
        return ("spawn", False, f"{leaked} child process(es) left running")
    return ("spawn", True, "one child, one frame, no strays")


def _check_vault_location() -> Check:
    """Assert the vault resolves to the user data directory, not next to the binary.

    A frozen onefile bundle unpacks to a temp directory that is deleted on
    exit; a vault written there would silently lose every saved credential.
    """
    from surftp.store import default_vault_path

    path = default_vault_path().resolve()
    root = _bundle_root()
    if path == root or root in path.parents:
        return ("vault", False, f"vault would be written inside the bundle: {path}")
    return ("vault", True, str(path))


def _check_imports() -> Check:
    """Import every transport and store module the app reaches at runtime.

    Guards the failure mode where a dependency imports its own distribution
    metadata (aioftp did exactly this, dying with ``PackageNotFoundError``) or
    a native wheel is left out of the bundle entirely.
    """
    import importlib

    modules = [
        "surftp.net.sftp",
        "surftp.net.ftp",
        "surftp.net.ssh",
        "surftp.net.scp",
        "surftp.net.hostkeys",
        "surftp.store.vault",
        "surftp.store.crypto",
        "surftp.transfer.engine",
        "surftp.widgets.terminal",
    ]
    for name in modules:
        importlib.import_module(name)
    return ("imports", True, f"{len(modules)} modules imported")


def run(stream=sys.stdout) -> int:
    """Run every check, print one PASS/FAIL line each, return a process exit code.

    Matches the reporting shape of ``tests/`` — one line per check, non-zero
    exit on any failure — so the release workflow needs no special parsing.
    """
    checks: list[Check] = []
    for label, run_check in (
        ("imports", _check_imports),
        ("ui", lambda: asyncio.run(_check_ui())),
        ("spawn", _check_spawn),
        ("vault", _check_vault_location),
    ):
        try:
            checks.append(run_check())
        except Exception as error:  # a crash here is itself the finding
            checks.append((label, False, f"{type(error).__name__}: {error}"))

    frozen = "frozen" if getattr(sys, "frozen", False) else "source"
    print(f"surftp self-check ({frozen}, {sys.platform}, python {sys.version.split()[0]})", file=stream)
    failed = 0
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}", file=stream)
        failed += 0 if ok else 1
    print("SELF-CHECK PASSED" if not failed else f"SELF-CHECK FAILED ({failed})", file=stream)
    return 1 if failed else 0
