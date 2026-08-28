"""FTP / FTPS implementation of the pane backend, over ``aioftp``.

Plain FTP sends the username and password in clear text. SURFTP supports it
because plenty of legacy servers offer nothing else, but the connect dialog
must show a visible warning whenever ``use_tls`` is off, and again when such a
password is saved to the vault. Silently treating FTP as equivalent to SFTP
would be the dishonest choice.
"""

from __future__ import annotations

import posixpath
from datetime import datetime

import aioftp

from surftp.fs.types import FileEntry, FileSystemError, sort_entries
from surftp.net.types import ConnectionProfile, Credential, NetworkError

CONNECT_TIMEOUT_SECONDS: int = 15


class FTPFileSystem:
    """A pane backend over one FTP control connection."""

    def __init__(self, client: aioftp.Client, profile: ConnectionProfile) -> None:
        """Wrap a logged-in client. Use :meth:`connect` rather than calling this."""
        self._client = client
        self._profile = profile

    @classmethod
    async def connect(cls, profile: ConnectionProfile, credential: Credential) -> FTPFileSystem:
        """Open and log in to an FTP (or FTPS, when ``use_tls``) server."""
        client = aioftp.Client(
            connection_timeout=CONNECT_TIMEOUT_SECONDS,
            socket_timeout=CONNECT_TIMEOUT_SECONDS,
            ssl=True if profile.use_tls else None,
        )
        try:
            await client.connect(profile.host, profile.port)
            await client.login(profile.username, credential.password or "")
        except aioftp.StatusCodeError as exc:
            raise NetworkError(
                f"{profile.username}@{profile.host} rejected the FTP login "
                f"(server said: {_first_line(exc)})."
            ) from exc
        except (OSError, aioftp.AIOFTPException) as exc:
            raise NetworkError(f"Cannot connect to {profile.host}:{profile.port}: {exc}") from exc
        return cls(client, profile)

    @property
    def label(self) -> str:
        """``user@host`` for the pane title."""
        return self._profile.display

    async def list_directory(self, path: str) -> list[FileEntry]:
        """List a remote directory as sorted ``FileEntry`` rows.

        ``aioftp`` prefers ``MLSD`` — a machine-readable listing with typed
        fields — and falls back to parsing ``LIST`` output only where the server
        lacks it. That ordering matters: ``LIST`` output is human-formatted and
        varies by server, so anything derived from it is a guess.
        """
        try:
            raw = await self._client.list(path or "/")
        except aioftp.StatusCodeError as exc:
            raise FileSystemError(f"Cannot list {path}: {_first_line(exc)}") from exc
        except (OSError, aioftp.AIOFTPException) as exc:
            raise FileSystemError(f"Connection lost while listing {path}: {exc}") from exc

        entries: list[FileEntry] = []
        for entry_path, info in raw:
            name = entry_path.name
            if name in (".", ".."):
                continue
            is_dir = str(info.get("type", "")) == "dir"
            entries.append(
                FileEntry(
                    name=name,
                    path=posixpath.join(path or "/", name),
                    is_dir=is_dir,
                    size=0 if is_dir else int(info.get("size", 0) or 0),
                    modified=_parse_modify(info.get("modify")),
                )
            )

        entries = sort_entries(entries)
        if path not in ("/", ""):
            entries.insert(
                0,
                FileEntry(name="..", path=await self.parent_of(path), is_dir=True, size=0, modified=0.0),
            )
        return entries

    async def parent_of(self, path: str) -> str:
        """Return the parent of a remote path, using POSIX rules."""
        return posixpath.dirname(path.rstrip("/")) or "/"

    async def is_directory(self, path: str) -> bool:
        """Return whether a remote path is a directory."""
        try:
            return await self._client.is_dir(path)
        except (OSError, aioftp.AIOFTPException):
            return False

    async def working_directory(self) -> str:
        """Return the directory the login landed in, for the pane's initial path."""
        if self._profile.remote_path:
            return self._profile.remote_path
        try:
            return str(await self._client.get_current_directory())
        except (OSError, aioftp.AIOFTPException):
            return "/"

    async def close(self) -> None:
        """Send QUIT and drop the control connection."""
        try:
            await self._client.quit()
        except (OSError, aioftp.AIOFTPException):
            self._client.close()  # server already gone; don't raise on the way out


def _parse_modify(value: object) -> float:
    """Convert an MLSD ``modify`` fact (``YYYYMMDDHHMMSS``) to a POSIX timestamp.

    Returns 0.0 when absent or unparseable — the formatter renders that as a
    blank cell rather than as the epoch.
    """
    if not value:
        return 0.0
    try:
        return datetime.strptime(str(value)[:14], "%Y%m%d%H%M%S").timestamp()
    except ValueError:
        return 0.0


def _first_line(exc: aioftp.StatusCodeError) -> str:
    """Extract the server's own message from an aioftp status error."""
    info = getattr(exc, "info", None)
    if info:
        return str(info[-1]).strip()
    return str(exc)
