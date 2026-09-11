"""CLI entry point for SURFTP."""

from __future__ import annotations

import argparse
import multiprocessing
import sys

from surftp import __version__
from surftp.app import SurfFTPApp


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, launch the TUI, and return the process exit code.

    Kept separate from ``SurfFTPApp`` so the app can also be run via
    ``textual run`` without argparse interfering.
    """
    # MUST be the first statement: under PyInstaller, ``spawn`` re-executes
    # this binary, so without this the shell's emulator child re-runs main()
    # and forks again — a process bomb. No-op outside a frozen build.
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(
        prog="surftp",
        description="Two-pane terminal client for SFTP, FTP, SSH and SCP.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    # Hidden: the release workflow's gate. It has to run *inside* the bundle,
    # so it is a flag on the shipped binary rather than a file in tests/.
    parser.add_argument(
        "--self-check", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)

    if args.self_check:
        from surftp.selfcheck import run

        return run()

    SurfFTPApp().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
