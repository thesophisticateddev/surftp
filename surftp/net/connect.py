"""Credential resolution and protocol dispatch — the single place a pane connects.

Two rules live here and nowhere else:

**Credential resolution order.** In descending priority:

1. an explicit credential typed into the dialog (a one-off connection; never
   persisted unless the user ticked "save"),
2. the vault's stored secret for the profile,
3. an interactive prompt (the caller supplies this via ``ask_secret``),
4. the ssh-agent, when the profile's method is ``AGENT``.

**Protocol dispatch.** SFTP and SCP both open an ``SSHSession`` and browse over
SFTP; FTP opens its own control connection. Keeping the ``match`` in one
function means a new protocol is one arm here rather than a search through the
UI layer.

This module sits beside ``ssh.py`` rather than inside it so that ``ssh.py``
never has to import the FTP client, and so the vault dependency stays out of
the SSH transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from surftp.fs.types import FileSystem
from surftp.net.ftp import FTPFileSystem
from surftp.net.sftp import SFTPFileSystem
from surftp.net.ssh import SSHSession, is_key_encrypted
from surftp.net.types import (
    AuthMethod,
    ConnectionProfile,
    Credential,
    NetworkError,
    Protocol,
    SecretKind,
)
from surftp.store.crypto import VaultLockedError
from surftp.store.vault import Vault

# Called with a human-readable prompt; returns the typed secret, or None if the
# user cancelled. Supplied by the UI so this module stays widget-free.
SecretAsker = Callable[[str], Awaitable[str | None]]


@dataclass(slots=True)
class RemoteConnection:
    """Everything a connected pane needs to hold on to.

    ``session`` is ``None`` for FTP, which has no SSH connection behind it. The
    pane keeps this object so disconnecting can close the channel *and* the
    connection underneath it, in that order.
    """

    profile: ConnectionProfile
    filesystem: FileSystem
    initial_path: str
    session: SSHSession | None = None

    async def close(self) -> None:
        """Tear down the channel first, then the connection it rides on."""
        if isinstance(self.filesystem, SFTPFileSystem):
            self.filesystem.close()
        elif isinstance(self.filesystem, FTPFileSystem):
            await self.filesystem.close()
        if self.session is not None:
            await self.session.close()


async def resolve_credential(
    profile: ConnectionProfile,
    vault: Vault | None,
    typed: Credential | None = None,
    ask_secret: SecretAsker | None = None,
) -> Credential:
    """Assemble the secret half of a connection, following the documented order.

    Never returns a partially-filled credential for a method that needs one: if
    no password or passphrase can be found, the caller gets ``NetworkError``
    rather than an empty string that the server would reject with a confusing
    "permission denied".
    """
    match profile.auth_method:
        case AuthMethod.AGENT:
            return Credential()

        case AuthMethod.PASSWORD:
            password = typed.password if typed else None
            if password is None:
                password = _stored(vault, profile, SecretKind.PASSWORD)
            if password is None and ask_secret is not None:
                password = await ask_secret(f"Password for {profile.display}")
            if password is None:
                raise NetworkError(f"No password available for {profile.display}.")
            return Credential(password=password)

        case AuthMethod.PEM_FILE | AuthMethod.PEM_STORED:
            key_data = typed.private_key_data if typed else None
            if key_data is None and profile.auth_method is AuthMethod.PEM_STORED:
                key_data = _stored(vault, profile, SecretKind.PRIVATE_KEY)
                if key_data is None:
                    raise NetworkError(
                        f"Profile '{profile.name}' uses a vault-stored key, but the vault holds none."
                    )

            passphrase = typed.key_passphrase if typed else None
            if passphrase is None:
                passphrase = _stored(vault, profile, SecretKind.KEY_PASSPHRASE)
            # Only prompt when the key really is encrypted, so users are not
            # trained to skip a passphrase field that is usually irrelevant.
            if (
                passphrase is None
                and ask_secret is not None
                and profile.pem_path
                and is_key_encrypted(profile.pem_path)
            ):
                passphrase = await ask_secret(f"Passphrase for {profile.pem_path}")
            return Credential(key_passphrase=passphrase, private_key_data=key_data)

    raise NetworkError(f"Unsupported authentication method: {profile.auth_method}")


def _stored(vault: Vault | None, profile: ConnectionProfile, kind: SecretKind) -> str | None:
    """Read a secret from the vault, treating "locked" as "nothing stored".

    Deliberate: a locked vault should fall through to prompting rather than
    abort the connection. The distinction still matters inside the vault, which
    is why that class refuses to blur the two itself.
    """
    if vault is None or profile.id is None or not vault.is_unlocked:
        return None
    try:
        return vault.get_secret(profile.id, kind)
    except VaultLockedError:
        return None


async def open_connection(profile: ConnectionProfile, credential: Credential) -> RemoteConnection:
    """Connect using ``profile`` and return a ready-to-browse ``RemoteConnection``."""
    match profile.protocol:
        case Protocol.SFTP | Protocol.SCP:
            session = await SSHSession.connect(profile, credential)
            try:
                filesystem = await session.start_sftp()
                path = await filesystem.realpath(profile.remote_path or ".")
            except Exception:
                await session.close()  # never leak the connection on a channel failure
                raise
            return RemoteConnection(profile, filesystem, path, session)

        case Protocol.FTP:
            ftp = await FTPFileSystem.connect(profile, credential)
            return RemoteConnection(profile, ftp, await ftp.working_directory())

    raise NetworkError(f"Unsupported protocol: {profile.protocol}")
