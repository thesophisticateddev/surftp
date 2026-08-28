"""The authenticated SSH connection, and the auth material that opens it.

One :class:`SSHSession` owns exactly one ``asyncssh`` connection. SFTP, SCP and
(later) tunnel port-forwards are all **channels multiplexed on it** — the user
authenticates once, and every subsequent feature opens a channel rather than a
second connection. That property is why this class exists at all instead of
each backend connecting for itself.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import asyncssh

from surftp.net import hostkeys
from surftp.net.errors import describe_connect_failure
from surftp.net.types import AuthMethod, ConnectionProfile, Credential, NetworkError

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from surftp.net.sftp import SFTPFileSystem

# How long to wait for a connection before giving up, and how often to probe an
# idle one. Without the keepalive a half-dead connection hangs a pane forever
# instead of surfacing an error.
CONNECT_TIMEOUT_SECONDS: int = 15
KEEPALIVE_INTERVAL_SECONDS: int = 30
KEEPALIVE_MAX_MISSED: int = 3


class PrivateKeyError(NetworkError):
    """A private key could not be loaded — missing, malformed, or wrong passphrase."""


def check_key_permissions(pem_path: str) -> str | None:
    """Return a warning when a key file is group- or world-readable, else ``None``.

    Mirrors OpenSSH's own check. This *warns* rather than refuses: the user may
    have a deliberate reason, and refusing to connect over a permission bit
    would be more obstructive than the risk warrants.
    """
    try:
        mode = os.stat(pem_path).st_mode
    except OSError:
        return None
    if mode & 0o077:
        return f"{pem_path} is readable by other users (mode {oct(mode & 0o777)}); consider chmod 600."
    return None


def load_private_key(
    pem_path: str | None,
    key_data: str | None,
    passphrase: str | None,
) -> asyncssh.SSHKey:
    """Load a private key from a ``.pem`` file or from vault-stored PEM text.

    ``asyncssh.read_private_key`` accepts PKCS#1/PKCS#8 PEM, OpenSSH and PuTTY
    formats, so an AWS-issued ``.pem`` works unchanged.

    The two failure modes are reported distinctly on purpose: a wrong
    passphrase and a malformed file demand completely different fixes, and
    collapsing both into "could not read key" sends users hunting the wrong one.
    Note that asyncssh signals them inconsistently — OpenSSH-format keys raise
    ``KeyEncryptionError`` while PKCS#8 keys raise ``KeyImportError`` with the
    reason only in the message — so both are classified by
    :func:`_is_passphrase_failure` rather than by exception type alone.
    """
    try:
        if key_data is not None:
            return asyncssh.import_private_key(key_data, passphrase=passphrase)
        if pem_path is None:
            raise PrivateKeyError("No private key was provided for key authentication.")
        return asyncssh.read_private_key(pem_path, passphrase=passphrase)
    except FileNotFoundError as exc:
        raise PrivateKeyError(f"Private key file not found: {pem_path}") from exc
    except (asyncssh.KeyEncryptionError, asyncssh.KeyImportError, ValueError) as exc:
        where = pem_path or "the stored key"
        if _is_passphrase_failure(exc):
            if passphrase:
                raise PrivateKeyError(f"Wrong passphrase for {where}.") from exc
            raise PrivateKeyError(
                f"{where} is passphrase-protected; a passphrase is required."
            ) from exc
        raise PrivateKeyError(f"Could not read {where}: {exc}") from exc


def _is_passphrase_failure(exc: BaseException) -> bool:
    """Whether a key-loading error is about the passphrase rather than the file itself.

    ``KeyEncryptionError`` always is. For PKCS#8 keys asyncssh raises a plain
    ``KeyImportError`` whose message is the only signal, so the wording is
    matched here — in one place, so a future asyncssh rewording is a one-line
    fix rather than a hunt.
    """
    if isinstance(exc, asyncssh.KeyEncryptionError):
        return True
    message = str(exc).lower()
    return "decrypt" in message or "passphrase" in message


def is_key_encrypted(pem_path: str) -> bool:
    """Whether a key file needs a passphrase, checked by trying to load it without one.

    Lets the dialog prompt for a passphrase only when one is actually needed,
    instead of asking every time and training users to ignore the field.
    """
    try:
        asyncssh.read_private_key(pem_path)
    except asyncssh.KeyEncryptionError:
        return True
    except (OSError, asyncssh.KeyImportError, ValueError):
        return False
    return False


class SSHSession:
    """One authenticated SSH connection. SFTP, SCP and tunnels are channels on it."""

    def __init__(self, connection: asyncssh.SSHClientConnection, profile: ConnectionProfile) -> None:
        """Wrap an established connection. Use :meth:`connect` rather than calling this."""
        self._connection = connection
        self._profile = profile

    @classmethod
    async def connect(cls, profile: ConnectionProfile, credential: Credential) -> SSHSession:
        """Authenticate to ``profile``'s host and return the live session.

        Host keys are always verified against ``known_hosts``; a failure is
        re-raised as ``HostKeyUnknown`` (ask the user) or ``HostKeyChanged``
        (hard stop) so the UI can tell the two apart.
        """
        options: dict[str, Any] = {
            "username": profile.username,
            "known_hosts": str(hostkeys.known_hosts_path()),
            "connect_timeout": CONNECT_TIMEOUT_SECONDS,
            "keepalive_interval": KEEPALIVE_INTERVAL_SECONDS,
            "keepalive_count_max": KEEPALIVE_MAX_MISSED,
        }

        match profile.auth_method:
            case AuthMethod.PASSWORD:
                options["password"] = credential.password or ""
                options["client_keys"] = None  # don't silently fall back to ~/.ssh keys
            case AuthMethod.PEM_FILE | AuthMethod.PEM_STORED:
                key = load_private_key(
                    profile.pem_path, credential.private_key_data, credential.key_passphrase
                )
                options["client_keys"] = [key]
            case AuthMethod.AGENT:
                options["agent_path"] = os.environ.get("SSH_AUTH_SOCK")
                if not options["agent_path"]:
                    raise NetworkError("SSH_AUTH_SOCK is not set — no ssh-agent to authenticate with.")

        try:
            connection = await asyncssh.connect(profile.host, profile.port, **options)
        except asyncssh.HostKeyNotVerifiable as exc:
            raise await hostkeys.classify_failure(profile.host, profile.port) from exc
        except NetworkError:
            raise  # already specific (e.g. PrivateKeyError); don't re-wrap
        except (OSError, asyncssh.Error) as exc:
            raise describe_connect_failure(exc, profile) from exc

        return cls(connection, profile)

    @property
    def profile(self) -> ConnectionProfile:
        """The profile this session was opened from."""
        return self._profile

    @property
    def connection(self) -> asyncssh.SSHClientConnection:
        """The raw asyncssh connection, for SCP and future port-forwarding."""
        return self._connection

    @property
    def is_connected(self) -> bool:
        """Whether the underlying transport is still up."""
        return not self._connection.is_closed()

    async def start_sftp(self) -> SFTPFileSystem:
        """Open an SFTP channel on this connection and wrap it as a pane backend.

        Note this opens a *channel*, not a connection: no second authentication.
        """
        from surftp.net.sftp import SFTPFileSystem

        try:
            client = await self._connection.start_sftp_client()
        except asyncssh.Error as exc:
            raise NetworkError(
                f"{self._profile.host} accepted the login but would not start SFTP "
                f"({exc}). The server may have the SFTP subsystem disabled."
            ) from exc
        return SFTPFileSystem(client, self._profile)

    async def home_directory(self) -> str:
        """Return the remote working directory to open the pane at.

        The profile's configured ``remote_path`` wins; otherwise ask the server
        where the login landed rather than assuming ``/`` or ``/home/<user>``.
        """
        if self._profile.remote_path:
            return self._profile.remote_path
        try:
            result = await self._connection.run("pwd", check=False, timeout=10)
            path = (result.stdout or "").strip() if isinstance(result.stdout, str) else ""
            if path.startswith("/"):
                return path
        except (asyncssh.Error, OSError):
            pass  # a restricted shell is normal; fall back below
        return "/"

    async def close(self) -> None:
        """Close the connection and wait for the transport to finish shutting down."""
        self._connection.close()
        await self._connection.wait_closed()
