"""SCP transfer over an existing :class:`~surftp.net.ssh.SSHSession`.

**SCP is a transfer protocol, not a browsable filesystem.** There is no
portable way to list a remote directory over SCP — the historical trick of
parsing ``ls`` output breaks on any non-English locale or unusual filename. So
an SCP *profile* browses over SFTP and transfers with ``scp`` on the same
connection.

That is the obvious wrong assumption for anyone reading "SCP" in the protocol
picker, which is exactly why it is spelled out here and surfaced in the UI when
a server has the SFTP subsystem disabled.

Transfers themselves belong to ``plan-transfers.md``; this module exists now so
the SCP profile type is honest about what it does, and provides the thin call
the transfer pass will build on.
"""

from __future__ import annotations

import asyncssh

from surftp.net.ssh import SSHSession
from surftp.net.types import NetworkError


class ScpTransfer:
    """File copy over the SSH connection an :class:`SSHSession` already holds."""

    def __init__(self, session: SSHSession) -> None:
        """Bind to a live session; opens no channel until a transfer runs."""
        self._session = session

    async def download(self, remote_path: str, local_path: str) -> None:
        """Copy a remote file to the local disk over SCP."""
        try:
            await asyncssh.scp((self._session.connection, remote_path), local_path)
        except (OSError, asyncssh.Error) as exc:
            raise NetworkError(f"SCP download of {remote_path} failed: {exc}") from exc

    async def upload(self, local_path: str, remote_path: str) -> None:
        """Copy a local file to the remote host over SCP."""
        try:
            await asyncssh.scp(local_path, (self._session.connection, remote_path))
        except (OSError, asyncssh.Error) as exc:
            raise NetworkError(f"SCP upload of {local_path} failed: {exc}") from exc
