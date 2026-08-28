"""Local-disk implementation of the pane backend.

This is the reference ``FileSystem``: every later backend (SFTP, FTP) must
reproduce the same semantics — sort order, parent link, and the
``FileSystemError`` error boundary.
"""

from __future__ import annotations

import os
import stat as stat_module

from surftp.fs.types import FileEntry, FileSystemError, sort_entries


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
                )
            )

        entries = sort_entries(entries)
        if path not in (os.path.sep, ""):
            parent = await self.parent_of(path)
            entries.insert(0, FileEntry(name="..", path=parent, is_dir=True, size=0, modified=0.0))
        return entries

    async def parent_of(self, path: str) -> str:
        """Return the parent path of ``path``, or ``path`` itself at the root."""
        parent = os.path.dirname(path)
        return parent if parent else path

    async def is_directory(self, path: str) -> bool:
        """Return whether ``path`` is a directory."""
        return os.path.isdir(path)
