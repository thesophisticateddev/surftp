"""Local-disk implementation of the pane backend.

This is the reference ``FileSystem``: every later backend (SFTP, FTP) must
reproduce the same semantics — sort order, parent link, and the
``FileSystemError`` error boundary.
"""

from __future__ import annotations

import asyncio
import os
import stat as stat_module
from pathlib import Path

from surftp.fs.types import FileEntry, FileSystemError, sort_entries


class _LocalReader:
    """Async wrapper around a local file opened for reading.

    Disk I/O is fast but not instant, and a 4 GB read on the event loop would
    freeze the UI exactly as a hung socket would. ``asyncio.to_thread`` keeps
    the event loop responsive.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._fp = open(path, "rb")  # noqa: SIM115

    async def read(self, size: int) -> bytes:
        """Read up to ``size`` bytes off the event loop."""
        return await asyncio.to_thread(self._fp.read, size)

    async def close(self) -> None:
        """Close the file handle."""
        await asyncio.to_thread(self._fp.close)


class _LocalWriter:
    """Async wrapper around a local file opened for writing.

    Writes go through ``asyncio.to_thread`` for the same reason reads do:
    the event loop must stay responsive during a large upload.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._fp = open(path, "wb")  # noqa: SIM115

    async def write(self, data: bytes) -> None:
        """Write ``data`` off the event loop."""
        await asyncio.to_thread(self._fp.write, data)

    async def close(self) -> None:
        """Flush and close the file handle."""
        await asyncio.to_thread(self._fp.close)


class LocalFileSystem:
    """Reads the machine's own disk. The reference implementation of the pane backend.

    The methods are ``async def`` but do no awaiting: local ``listdir``/``stat``
    complete in microseconds, so paying for a thread would cost more than it
    saves. The async signature exists to match the ``FileSystem`` protocol that
    the network backends genuinely need.
    """

    @property
    def label(self) -> str:
        """Backend name for the pane title."""
        return "local"

    async def list_directory(self, path: str) -> list[FileEntry]:
        """List ``path`` as sorted ``FileEntry`` rows.

        Directories first, then files, each case-insensitively by name. A
        ``..`` parent link is prepended unless already at the filesystem
        root. Unreadable target directories raise ``FileSystemError``; a
        per-entry ``stat()`` failure (e.g. a broken symlink) is skipped.
        """
        try:
            names = os.listdir(path)
        except (PermissionError, FileNotFoundError, NotADirectoryError, OSError) as exc:
            raise FileSystemError(f"Cannot list {path}: {exc.strerror or exc}") from exc

        entries: list[FileEntry] = []
        for name in names:
            full = os.path.join(path, name)
            try:
                st = os.stat(full, follow_symlinks=False)
            except OSError:
                # Broken symlink or raced-away entry: skip, don't die.
                continue
            is_dir = stat_module.S_ISDIR(st.st_mode)
            entries.append(
                FileEntry(
                    name=name,
                    path=full,
                    is_dir=is_dir,
                    size=0 if is_dir else st.st_size,
                    modified=st.st_mtime,
                    permissions=st.st_mode,
                )
            )

        entries = sort_entries(entries)
        if path not in (os.path.sep, ""):
            parent = await self.parent_of(path)
            entries.insert(0, FileEntry(name="..", path=parent, is_dir=True, size=0, modified=0.0))
        return entries

    def join_path(self, base: str, name: str) -> str:
        """Join using the client's own path rules.

        ``os.path`` is correct here and *only* here: this filesystem really is
        the local machine, so the local platform's separator is the right one
        (``ntpath`` on Windows, ``posixpath`` elsewhere). Remote backends must
        never reason this way — see ``remote_join``.
        """
        return os.path.join(base, name)

    async def parent_of(self, path: str) -> str:
        """Return the parent path of ``path``, or ``path`` itself at the root."""
        parent = os.path.dirname(path)
        return parent if parent else path

    async def is_directory(self, path: str) -> bool:
        """Return whether ``path`` is a directory."""
        return os.path.isdir(path)

    async def stat(self, path: str) -> FileEntry:
        """Return a ``FileEntry`` for ``path``; raise ``FileSystemError`` if it does not exist."""
        try:
            st = os.stat(path, follow_symlinks=False)
        except (FileNotFoundError, OSError) as exc:
            raise FileSystemError(f"Cannot stat {path}: {exc.strerror or exc}") from exc
        is_dir = stat_module.S_ISDIR(st.st_mode)
        return FileEntry(
            name=os.path.basename(path),
            path=path,
            is_dir=is_dir,
            size=0 if is_dir else st.st_size,
            modified=st.st_mtime,
            permissions=st.st_mode,
        )

    async def open_read(self, path: str) -> _LocalReader:
        """Open ``path`` for reading."""
        try:
            return _LocalReader(path)
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise FileSystemError(f"Cannot open {path} for reading: {exc.strerror or exc}") from exc

    async def open_write(self, path: str, size_hint: int = 0) -> _LocalWriter:
        """Open ``path`` for writing (truncating)."""
        try:
            return _LocalWriter(path)
        except (PermissionError, OSError) as exc:
            raise FileSystemError(f"Cannot open {path} for writing: {exc.strerror or exc}") from exc

    async def make_directory(self, path: str) -> None:
        """Create ``path`` as a directory (including parents)."""
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
        except (PermissionError, OSError) as exc:
            raise FileSystemError(f"Cannot create directory {path}: {exc.strerror or exc}") from exc

    async def remove(self, path: str) -> None:
        """Delete a file at ``path``."""
        try:
            os.remove(path)
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise FileSystemError(f"Cannot remove {path}: {exc.strerror or exc}") from exc

    async def rename(self, src: str, dst: str) -> None:
        """Rename ``src`` to ``dst``."""
        try:
            os.rename(src, dst)
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise FileSystemError(f"Cannot rename {src} to {dst}: {exc.strerror or exc}") from exc
