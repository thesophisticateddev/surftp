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

import posixpath
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
    permissions: int = 0  # full POSIX st_mode where known; 0 = "backend did not say"


@runtime_checkable
class AsyncFileReader(Protocol):
    """A readable file handle returned by ``FileSystem.open_read``.

    Backends may wrap a local file, an SFTP channel or an FTP data stream —
    the transfer engine only needs ``read`` and ``close``.
    """

    async def read(self, size: int) -> bytes:
        """Read up to ``size`` bytes; return ``b""`` at EOF."""
        ...

    async def close(self) -> None:
        """Release the handle."""
        ...


@runtime_checkable
class AsyncFileWriter(Protocol):
    """A writable file handle returned by ``FileSystem.open_write``.

    The transfer engine writes chunks and closes on completion. A partial
    write that is not followed by ``close`` leaves a ``.surftp-partial-*``
    file that the engine cleans up on cancellation.
    """

    async def write(self, data: bytes) -> None:
        """Write ``data`` to the file."""
        ...

    async def close(self) -> None:
        """Flush and release the handle."""
        ...


@runtime_checkable
class FileSystem(Protocol):
    """The protocol-agnostic backend interface every pane talks to.

    The browsing methods (``list_directory``, ``parent_of``, ``is_directory``)
    are what the panes use. The transfer methods (``open_read``, ``open_write``,
    ``make_directory``, ``stat``) are what the transfer engine uses. Keeping
    both in one protocol means the engine copies between two ``FileSystem``
    objects and never learns which protocols it is bridging.
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

    def join_path(self, base: str, name: str) -> str:
        """Join ``base`` and ``name`` using *this* filesystem's path rules.

        Synchronous: joining is string arithmetic, never I/O.

        This belongs to the backend for the same reason ``parent_of`` does — the
        backend is the only component that knows its own path syntax. A caller
        that inspects the string and guesses is guessing about something already
        known for certain, and will guess wrong on the cases that matter.
        """
        ...

    async def is_directory(self, path: str) -> bool:
        """Return whether ``path`` is a directory."""
        ...

    async def stat(self, path: str) -> FileEntry:
        """Return a ``FileEntry`` for ``path``; raise ``FileSystemError`` if it does not exist."""
        ...

    async def open_read(self, path: str) -> AsyncFileReader:
        """Open ``path`` for reading; raise ``FileSystemError`` on failure."""
        ...

    async def open_write(self, path: str, size_hint: int = 0) -> AsyncFileWriter:
        """Open ``path`` for writing (truncating); raise ``FileSystemError`` on failure."""
        ...

    async def make_directory(self, path: str) -> None:
        """Create ``path`` as a directory (including parents); raise ``FileSystemError`` on failure."""
        ...

    async def remove(self, path: str) -> None:
        """Delete a file at ``path``; raise ``FileSystemError`` on failure."""
        ...

    async def rename(self, src: str, dst: str) -> None:
        """Rename ``src`` to ``dst``; raise ``FileSystemError`` on failure."""
        ...


def remote_join(base: str, name: str) -> str:
    """Join a path on a remote filesystem whose wire format is POSIX.

    Shared by the SFTP and FTP backends. Both protocols specify ``/`` as the
    separator, so POSIX rules are the default and the common case.

    The exception is a server that reports native Windows paths (``C:\\Users``
    or a ``\\\\server\\share`` UNC). Joining those with ``/`` yields a mixed
    separator like ``C:\\Users/file.txt``. They are joined with ``ntpath``
    **explicitly** — never ``os.path``, which is the *client's* module and
    silently becomes ``posixpath`` on Linux and macOS, turning the Windows
    branch into a no-op on every non-Windows client.

    Note that OpenSSH-for-Windows usually reports ``/C:/Users``-style paths,
    which are POSIX-shaped and correctly take the default branch.
    """
    if _is_windows_style(base):
        import ntpath

        return ntpath.join(base, name)
    return posixpath.join(base, name)


def _is_windows_style(path: str) -> bool:
    """Whether ``path`` uses native Windows syntax (drive letter or UNC prefix).

    Deliberately narrow: only a drive-letter root (``C:\\``) or a UNC prefix
    (``\\\\host``) counts. A bare backslash elsewhere is not enough — it is a
    legal character in a POSIX filename, and treating it as a platform signal
    would corrupt paths that merely contain one.
    """
    if path.startswith("\\\\"):
        return True
    return len(path) >= 3 and path[1] == ":" and path[2] in "\\/" and path[0].isalpha()


def display_name(path: str) -> str:
    """Basename of a path from *either* platform, for labels only.

    Used where a path is being shown to the user rather than acted on, so it
    must not assume the separator: ``posixpath.basename`` on ``C:\\a\\b.txt``
    returns the whole string, which shows the user a full path where a filename
    belongs. Splitting on both separators is right for display and wrong for
    anything else — do not use this to build paths, use ``FileSystem.join_path``.
    """
    return path.replace("\\", "/").rstrip("/").rpartition("/")[2] or path


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


def format_permissions(permissions: int, is_dir: bool) -> str:
    """Render a permissions column cell as a 10-character POSIX mode string.

    Returns the familiar ``drwxr-xr-x`` format. Honors setuid/setgid/sticky
    bits (``rws``, ``rwt``). When ``permissions == 0``, returns an empty string
    rather than ``----------`` — a blank cell says "unknown"; ten dashes falsely
    asserts "no permissions at all", and FTP servers that omit the mode would
    make every file look unreadable.

    The type character comes from ``stat.S_IFMT`` when the mode carries type
    bits, else falls back to ``d``/``-`` from ``is_dir``.
    """
    import stat

    if permissions == 0:
        return ""

    # Type character
    file_type = stat.S_IFMT(permissions)
    if file_type == stat.S_IFDIR:
        type_char = "d"
    elif file_type == stat.S_IFLNK:
        type_char = "l"
    elif file_type == stat.S_IFSOCK:
        type_char = "s"
    elif file_type == stat.S_IFIFO:
        type_char = "p"
    elif file_type == stat.S_IFBLK:
        type_char = "b"
    elif file_type == stat.S_IFCHR:
        type_char = "c"
    else:
        type_char = "-" if not is_dir else "d"

    # Permission bits
    mode = permissions & 0o7777
    owner = (mode >> 6) & 0o7
    group = (mode >> 3) & 0o7
    other = mode & 0o7

    def triplet(bits: int, special: bool, special_char: str) -> str:
        """Render one rwx triplet with optional special bit."""
        r = "r" if bits & 4 else "-"
        w = "w" if bits & 2 else "-"
        if special:
            x = special_char if bits & 1 else special_char.upper()
        else:
            x = "x" if bits & 1 else "-"
        return r + w + x

    # Setuid/setgid/sticky
    setuid = bool(mode & stat.S_ISUID)
    setgid = bool(mode & stat.S_ISGID)
    sticky = bool(mode & stat.S_ISVTX)

    owner_str = triplet(owner, setuid, "s")
    group_str = triplet(group, setgid, "s")
    other_str = triplet(other, sticky, "t")

    return type_char + owner_str + group_str + other_str
