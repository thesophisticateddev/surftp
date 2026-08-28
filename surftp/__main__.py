"""CLI entry point for SURFTP."""

from __future__ import annotations

import argparse
import sys

from surftp import __version__
from surftp.app import SurfFTPApp


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, launch the TUI, and return the process exit code.

    Kept separate from ``SurfFTPApp`` so the app can also be run via
    ``textual run`` without argparse interfering.
    """
    parser = argparse.ArgumentParser(
        prog="surftp",
        description="Two-pane terminal client for SFTP, FTP, SSH and SCP.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.parse_args(argv)

    SurfFTPApp().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
