"""SFTP implementation of the pane backend.

Reproduces ``LocalFileSystem``'s semantics exactly — same sort order, same
``..`` parent link, same single error type — so ``FilePane`` cannot tell the
difference between a local and a remote pane.
"""

from __future__ import annotations

import posixpath
import stat as stat_module

import asyncssh

from surftp.fs.types import FileEntry, FileSystemError, sort_entries
from surftp.net.types import ConnectionProfile


class _SFTPReader:
    """Async wrapper around an SFTP file opened for reading.

    asyncssh's SFTP file objects already pipeline reads with a configurable
    window (``block_size``, ``max_requests``), so this is a thin adapter.
    """

    def __init__(self, fp: asyncssh.SFTPClientFile) -> None:
        self._fp = fp

    async def read(self, size: int) -> bytes:
        """Read up to ``size`` bytes; return ``b""`` at EOF."""
        data = await self._fp.read(size)
        return data if isinstance(data, bytes) else b""

    async def close(self) -> None:
        """Close the SFTP file handle."""
        self._fp.close()


class _SFTPWriter:
    """Async wrapper around an SFTP file opened for writing."""

    def __init__(self, fp: asyncssh.SFTPClientFile) -> None:
        self._fp = fp

    async def write(self, data: bytes) -> None:
        """Write ``data`` to the remote file."""
        await self._fp.write(data)

    async def close(self) -> None:
        """Close the SFTP file handle."""
        self._fp.close()


class SFTPFileSystem:
    """A pane backend over one SFTP channel of an established ``SSHSession``."""

    def __init__(self, client: asyncssh.SFTPClient, profile: ConnectionProfile) -> None:
        """Wrap an open SFTP client. Created by :meth:`SSHSession.start_sftp`."""
        self._client = client
        self._profile = profile

    @property
    def label(self) -> str:
        """``user@host`` for the pane title, so a remote pane is obviously remote."""
        return self._profile.display

    async def list_directory(self, path: str) -> list[FileEntry]:
        """List a remote directory as sorted ``FileEntry`` rows.

        Uses ``readdir``, which returns each name *with* its attributes in the
        same round trip. Calling ``stat`` per entry instead would turn a
        1000-file directory into 1000 round trips — the classic way to make a
        remote file browser feel broken.
        """
        try:
            names = await self._client.readdir(path)
        except asyncssh.SFTPError as exc:
            raise FileSystemError(f"Cannot list {path}: {_sftp_reason(exc)}") from exc
        except (OSError, asyncssh.Error) as exc:
            raise FileSystemError(f"Connection lost while listing {path}: {exc}") from exc

        entries: list[FileEntry] = []
        for item in names:
            filename = item.filename
            if isinstance(filename, bytes):
                filename = filename.decode("utf-8", "replace")
            if filename in (".", ".."):
                continue  # the parent link is added below, uniformly with the local backend
            attrs = item.attrs
            permissions = attrs.permissions or 0
            is_dir = stat_module.S_ISDIR(permissions)
            entries.append(
                FileEntry(
                    name=filename,
                    path=posixpath.join(path, filename),
                    is_dir=is_dir,
                    size=0 if is_dir else int(attrs.size or 0),
                    modified=float(attrs.mtime or 0),
                    permissions=permissions,
                )
            )

        entries = sort_entries(entries)
        if path != "/":
            entries.insert(
                0,
                FileEntry(name="..", path=await self.parent_of(path), is_dir=True, size=0, modified=0.0),
            )
        return entries

    async def parent_of(self, path: str) -> str:
        """Return the parent of a remote path, using POSIX rules regardless of local OS.

        ``posixpath`` explicitly, not ``os.path``: a Windows client browsing a
        Linux server must not build ``C:\\``-style paths.
        """
        parent = posixpath.dirname(path.rstrip("/")) or "/"
        return parent

    async def is_directory(self, path: str) -> bool:
        """Return whether a remote path is a directory."""
        try:
            return await self._client.isdir(path)
        except (asyncssh.SFTPError, OSError, asyncssh.Error):
            return False

    async def realpath(self, path: str) -> str:
        """Canonicalise a remote path, expanding ``.`` to the login directory."""
        try:
            resolved = await self._client.realpath(path)
        except (asyncssh.SFTPError, OSError, asyncssh.Error):
            return path
        return resolved.decode("utf-8", "replace") if isinstance(resolved, bytes) else str(resolved)

    async def stat(self, path: str) -> FileEntry:
        """Return a ``FileEntry`` for ``path``; raise ``FileSystemError`` if it does not exist."""
        try:
            attrs = await self._client.stat(path)
        except asyncssh.SFTPError as exc:
            raise FileSystemError(f"Cannot stat {path}: {_sftp_reason(exc)}") from exc
        except (OSError, asyncssh.Error) as exc:
            raise FileSystemError(f"Connection lost while stat-ing {path}: {exc}") from exc
        permissions = attrs.permissions or 0
        is_dir = stat_module.S_ISDIR(permissions)
        return FileEntry(
            name=posixpath.basename(path),
            path=path,
            is_dir=is_dir,
            size=0 if is_dir else int(attrs.size or 0),
            modified=float(attrs.mtime or 0),
            permissions=permissions,
        )

    async def open_read(self, path: str) -> _SFTPReader:
        """Open ``path`` for reading."""
        try:
            fp = await self._client.open(path, "r")
        except asyncssh.SFTPError as exc:
            raise FileSystemError(f"Cannot open {path} for reading: {_sftp_reason(exc)}") from exc
        except (OSError, asyncssh.Error) as exc:
            raise FileSystemError(f"Connection lost while opening {path}: {exc}") from exc
        return _SFTPReader(fp)

    async def open_write(self, path: str, size_hint: int = 0) -> _SFTPWriter:
        """Open ``path`` for writing (truncating)."""
        try:
            fp = await self._client.open(path, "w")
        except asyncssh.SFTPError as exc:
            raise FileSystemError(f"Cannot open {path} for writing: {_sftp_reason(exc)}") from exc
        except (OSError, asyncssh.Error) as exc:
            raise FileSystemError(f"Connection lost while opening {path}: {exc}") from exc
        return _SFTPWriter(fp)

    async def make_directory(self, path: str) -> None:
        """Create ``path`` as a directory (including parents)."""
        try:
            await self._client.makedirs(path)
        except asyncssh.SFTPError as exc:
            raise FileSystemError(f"Cannot create directory {path}: {_sftp_reason(exc)}") from exc
        except (OSError, asyncssh.Error) as exc:
            raise FileSystemError(f"Connection lost while creating {path}: {exc}") from exc

    async def remove(self, path: str) -> None:
        """Delete a file at ``path``."""
        try:
            await self._client.remove(path)
        except asyncssh.SFTPError as exc:
            raise FileSystemError(f"Cannot remove {path}: {_sftp_reason(exc)}") from exc
        except (OSError, asyncssh.Error) as exc:
            raise FileSystemError(f"Connection lost while removing {path}: {exc}") from exc

    async def rename(self, src: str, dst: str) -> None:
        """Rename ``src`` to ``dst``."""
        try:
            await self._client.rename(src, dst)
        except asyncssh.SFTPError as exc:
            raise FileSystemError(f"Cannot rename {src} to {dst}: {_sftp_reason(exc)}") from exc
        except (OSError, asyncssh.Error) as exc:
            raise FileSystemError(f"Connection lost while renaming {src}: {exc}") from exc

    def close(self) -> None:
        """Close the SFTP channel, leaving the parent SSH connection open.

        The connection outlives the channel because the tunnel may still be
        using it — closing the session is ``SSHSession.close``'s job, not this.
        """
        self._client.exit()


def _sftp_reason(exc: asyncssh.SFTPError) -> str:
    """Render an SFTP status code as something a user can act on."""
    reason = exc.reason or str(exc)
    if exc.code == asyncssh.FX_PERMISSION_DENIED:
        return "permission denied"
    if exc.code == asyncssh.FX_NO_SUCH_FILE:
        return "no such file"
    return reason
