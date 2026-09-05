"""Credential resolution and protocol dispatch — the single place a connection starts.

Two rules live here and nowhere else:

**Credential resolution order.** In descending priority:

1. an explicit credential typed into the dialog (a one-off connection; never
   persisted unless the user ticked "save"),
2. the vault's stored secret for the profile,
3. the in-memory per-session credential cache (a one-off secret typed for an
   earlier connection to the same profile, so a dedicated SSH connection does
   not prompt twice — see ``credential_cache.py``),
4. an interactive prompt (the caller supplies this via ``ask_secret``),
5. the ssh-agent, when the profile's method is ``AGENT``.

**Protocol dispatch.** SFTP and SCP open an ``SSHSession`` and browse over
SFTP; FTP opens its own control connection; ``Protocol.SSH`` opens an
``SSHSession`` with **no file pane** — the connection exists for a terminal, a
tunnel or a command, and that is why ``RemoteConnection.filesystem`` is
``None`` for it. Keeping the ``match`` in one function means a new protocol is
one arm here rather than a search through the UI layer.

This module sits beside ``ssh.py`` rather than inside it so that ``ssh.py``
never has to import the FTP client, and so the vault dependency stays out of
the SSH transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from surftp.fs.types import FileSystem
from surftp.net.credential_cache import get_credential_cache
from surftp.net.ftp import FTPFileSystem
from surftp.net.manager import get_manager
from surftp.net.sftp import SFTPFileSystem
from surftp.net.ssh import SSHSession, is_key_encrypted
from surftp.net.types import (
    AuthMethod,
    ConnectionProfile,
    Credential,
    NetworkError,
    OpenConnectionError,
    Protocol,
    SecretKind,
    key_passphrase_kind,
)
from surftp.store.crypto import VaultLockedError
from surftp.store.vault import Vault

# Called with a human-readable prompt; returns the typed secret, or None if the
# user cancelled. Supplied by the UI so this module stays widget-free.
SecretAsker = Callable[[str], Awaitable[str | None]]


@dataclass(slots=True)
class RemoteConnection:
    """Everything a connected session needs to hold on to.

    ``filesystem`` is ``None`` for ``Protocol.SSH`` — the connection exists
    for a terminal, a tunnel or a command, not for browsing, so the two call
    sites that attach a filesystem to a pane must refuse such a connection
    rather than crash on ``None``. ``session`` is ``None`` for FTP, which has
    no SSH connection behind it.
    """

    profile: ConnectionProfile
    filesystem: FileSystem | None
    initial_path: str
    session: SSHSession | None = None

    @property
    def is_ssh_session(self) -> bool:
        """Whether this connection is an SSH session with no pane backend."""
        return self.filesystem is None and self.session is not None

    async def close(self) -> None:
        """Tear down the channel first, then the connection it rides on.

        SSH connections are reference-counted by the manager: closing one pane
        of several that share a session only repays that pane's reference, and
        the connection closes when the last consumer releases it.
        """
        if isinstance(self.filesystem, SFTPFileSystem):
            self.filesystem.close()
        elif isinstance(self.filesystem, FTPFileSystem):
            await self.filesystem.close()
        if self.session is not None:
            await get_manager().release(self.session)


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
    cache = get_credential_cache()
    # A dedicated SSH connection authenticates separately; if the user typed a
    # one-off secret for an earlier connection to the same profile, it is held
    # here so there is no second prompt.
    typed = typed or cache.get(profile)
    prompted = False

    match profile.auth_method:
        case AuthMethod.AGENT:
            return Credential()

        case AuthMethod.PASSWORD:
            password = typed.password if typed else None
            if password is None:
                password = _stored(vault, profile, SecretKind.PASSWORD)
            if password is None and ask_secret is not None:
                password = await ask_secret(f"Password for {profile.display}")
                prompted = True
            if password is None:
                raise NetworkError(f"No password available for {profile.display}.")
            credential = Credential(password=password)
            if prompted or typed is not None:
                cache.put(profile, credential)
            return credential

        case AuthMethod.PEM_FILE:
            key_paths = profile.key_paths
            if not key_paths:
                raise NetworkError(
                    f"Profile '{profile.name}' uses key authentication but has no key file."
                )
            passphrases: list[str | None] = []
            for index, path in enumerate(key_paths):
                passphrase = typed.passphrase_for(index) if typed else None
                used_vault = False
                if passphrase is None:
                    passphrase = _stored(vault, profile, key_passphrase_kind(index))
                    used_vault = passphrase is not None
                if passphrase is None and index == 0:
                    # The single-key row written before indexed kinds existed.
                    passphrase = _stored(vault, profile, SecretKind.KEY_PASSPHRASE)
                    used_vault = passphrase is not None
                if (
                    passphrase is None
                    and ask_secret is not None
                    and path
                    and is_key_encrypted(path)
                ):
                    passphrase = await ask_secret(f"Passphrase for {path}")
                    prompted = True
                passphrases.append(passphrase)
            credential = Credential(key_passphrases=tuple(passphrases))
            if prompted or typed is not None:
                cache.put(profile, credential)
            return credential

        case AuthMethod.PEM_STORED:
            key_data = typed.private_key_data if typed else None
            if key_data is None:
                key_data = _stored(vault, profile, SecretKind.PRIVATE_KEY)
                if key_data is None:
                    raise NetworkError(
                        f"Profile '{profile.name}' uses a vault-stored key, but the vault holds none."
                    )

            passphrase = typed.passphrase_for(0) if typed else None
            if passphrase is None:
                passphrase = _stored(vault, profile, key_passphrase_kind(0))
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
                prompted = True
            credential = Credential(key_passphrase=passphrase, private_key_data=key_data)
            if prompted or typed is not None:
                cache.put(profile, credential)
            return credential

    raise NetworkError(f"Unsupported authentication method: {profile.auth_method}")


def _stored(
    vault: Vault | None, profile: ConnectionProfile, kind: SecretKind | str
) -> str | None:
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
    """Connect using ``profile`` and return a ready connection.

    For SFTP/SCP/FTP the connection is ready to browse; for ``Protocol.SSH``
    the ``RemoteConnection`` carries ``filesystem=None`` — it exists for a
    terminal, a tunnel or a command instead. All SSH connections are acquired
    through the connection manager, which decides whether to share one.
    """
    match profile.protocol:
        case Protocol.SFTP | Protocol.SCP:
            session = await get_manager().acquire(profile, credential)
            try:
                filesystem = await session.start_sftp()
                path = await filesystem.realpath(profile.remote_path or ".")
            except Exception:
                await get_manager().release(session)  # never leak the connection on a channel failure
                raise OpenConnectionError(f"Error occured while opening a connection")
            return RemoteConnection(profile, filesystem, path, session)

        case Protocol.SSH:
            session = await get_manager().acquire(profile, credential)
            return RemoteConnection(profile, None, "", session)

        case Protocol.FTP:
            ftp = await FTPFileSystem.connect(profile, credential)
            return RemoteConnection(profile, ftp, await ftp.working_directory())

    raise NetworkError(f"Unsupported protocol: {profile.protocol}")