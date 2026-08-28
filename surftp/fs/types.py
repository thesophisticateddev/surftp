"""Protocol-agnostic row model and backend interface shared by every pane.

This module is the seam between the UI and the filesystems: widgets render
``FileEntry`` objects and never touch ``os`` or ``pathlib`` themselves, so a
remote backend (SFTP, FTP, SCP) can produce the same rows without any widget
changes.

The ``FileSystem`` protocol is **async**. Local disk is fast enough to be
synchronous, but a network listing can hang for seconds, so the contract is
async everywhere and ``LocalFileSystem`` simply returns immediately. One code
path in the pane beats branching on whether a backend returns a coroutine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


class FileSystemError(Exception):
    """The single error type the UI must handle.

    Backend-specific errors (OS errors, ``asyncssh.SFTPError``, aioftp status
    codes) are caught at their layer boundary and re-raised as this, so the
    panes have exactly one exception type to deal with.
    """


@dataclass(frozen=True, slots=True)
class FileEntry:
    """A single row in a file pane, independent of which protocol produced it."""

    name: str  # basename only; ".." for the parent link
    path: str  # full path on the owning filesystem
    is_dir: bool
    size: int  # bytes; 0 for directories
    modified: float  # POSIX timestamp


@runtime_checkable
class FileSystem(Protocol):
    """The protocol-agnostic backend interface every pane talks to.

    Three methods is the whole contract; adding a remote backend means
    implementing exactly these. ``label`` is what the pane shows in its border
    title so a remote pane can render ``user@host`` instead of a bare path.
    """

    @property
    def label(self) -> str:
        """Short human-readable name of this backend, for the pane title."""
        ...

    async def list_directory(self, path: str) -> list[FileEntry]:
        """List ``path`` as sorted ``FileEntry`` rows, or raise ``FileSystemError``."""
        ...

    async def parent_of(self, path: str) -> str:
        """Return the parent of ``path``, or ``path`` itself at the root."""
        ...

    async def is_directory(self, path: str) -> bool:
        """Return whether ``path`` is a directory."""
        ...


def sort_entries(entries: list[FileEntry]) -> list[FileEntry]:
    """Apply the canonical pane sort: directories first, then files, case-insensitive.

    Shared by every backend so a remote listing is ordered identically to a
    local one — commander users navigate by muscle memory, and an SFTP pane
    that sorted differently from the local pane would be its own bug.
    """
    return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower()))


def format_size(size: int, is_dir: bool) -> str:
    """Render a size column cell.

    Directories show a fixed ``<DIR>`` marker; files show a human-readable
    byte count. Lives here, not in the widget, so every backend renders
    identically.
    """
    if is_dir:
        return "<DIR>"
    units = ["B", "K", "M", "G", "T", "P"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{int(value)} P"  # pragma: no cover - unreachable sentinel


def format_modified(timestamp: float) -> str:
    """Render a modified-time column cell as fixed-width ``YYYY-MM-DD HH:MM``.

    Fixed width keeps the columns aligned regardless of locale or backend. A
    zero timestamp (the ``..`` link, or a server that omits mtime) renders
    blank rather than as the epoch.
    """
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
