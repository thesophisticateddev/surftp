"""Connection data model shared by the vault, the dialogs and the backends.

Nothing here holds a secret. A ``ConnectionProfile`` is the *addressable* part
of a target — host, user, which auth method to use — and secrets live in the
vault keyed by the profile id. Keeping them apart is what lets the profile
picker render its list while the vault is still locked.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class Protocol(StrEnum):
    """Wire protocol used to reach a host."""

    SFTP = "sftp"
    SCP = "scp"
    FTP = "ftp"


class AuthMethod(StrEnum):
    """How SURFTP proves who it is to the remote host."""

    PASSWORD = "password"  # username + password
    PEM_FILE = "pem_file"  # key stays on disk; the vault holds only the path
    PEM_STORED = "pem_stored"  # key material imported into the vault, encrypted
    AGENT = "agent"  # delegate to the running ssh-agent via SSH_AUTH_SOCK


class SecretKind(StrEnum):
    """The kinds of secret a profile can own, one row each in ``secrets``."""

    PASSWORD = "password"
    KEY_PASSPHRASE = "key_passphrase"
    PRIVATE_KEY = "private_key"


DEFAULT_PORTS: dict[Protocol, int] = {
    Protocol.SFTP: 22,
    Protocol.SCP: 22,
    Protocol.FTP: 21,
}


class NetworkError(Exception):
    """The single error type the connection layer raises.

    Every underlying library error (asyncssh, aioftp, socket, DNS) is mapped to
    this at the layer boundary with a message that names the *actual* cause —
    see ``surftp.net.errors``. A bare "connection failed" is the failure mode
    that makes a client like this unusable.
    """


class HostKeyChanged(NetworkError):
    """The host presented a different key than the one in known_hosts.

    Its own type because it is the one connection failure with no "try again"
    path in the UI: it is either a server rebuild or an active attack, and both
    demand the user look at known_hosts themselves.
    """


class HostKeyUnknown(NetworkError):
    """The host is not in known_hosts yet; the UI must ask the user to trust it."""

    def __init__(self, host: str, port: int, fingerprint: str, key_type: str) -> None:
        """Carry the details the trust prompt has to display."""
        super().__init__(f"Unknown host key for {host}:{port} ({key_type})")
        self.host = host
        self.port = port
        self.fingerprint = fingerprint
        self.key_type = key_type


@dataclass(frozen=True, slots=True)
class ConnectionProfile:
    """A saved target. Contains no secrets — those live in the vault, keyed by id.

    ``id`` is ``None`` until the profile has been written to the vault, which
    is also how a one-off (unsaved) connection is represented.
    """

    name: str
    protocol: Protocol
    host: str
    username: str
    auth_method: AuthMethod
    port: int = 0  # 0 means "use the protocol default"; resolved by __post_init__
    pem_path: str | None = None
    remote_path: str | None = None
    use_tls: bool = False  # FTPS; ignored for SSH-based protocols
    id: int | None = None

    def __post_init__(self) -> None:
        """Fill in the protocol's default port when none was given."""
        if not self.port:
            object.__setattr__(self, "port", DEFAULT_PORTS[self.protocol])

    @property
    def display(self) -> str:
        """``user@host:port`` — the pane title and the picker's second column."""
        return f"{self.username}@{self.host}:{self.port}"

    @property
    def is_ssh(self) -> bool:
        """Whether this profile is reached over SSH (SFTP and SCP both are)."""
        return self.protocol in (Protocol.SFTP, Protocol.SCP)

    def with_id(self, profile_id: int) -> ConnectionProfile:
        """Return a copy carrying the id assigned by the vault on save."""
        return replace(self, id=profile_id)


@dataclass(frozen=True, slots=True)
class Credential:
    """The secret half of a connection attempt, assembled just before connecting.

    Held only for the duration of a connect call. It is never persisted from
    here — saving is an explicit, separate vault write, so a one-off connection
    cannot silently leave a password on disk.
    """

    password: str | None = None
    key_passphrase: str | None = None
    private_key_data: str | None = None  # PEM text, for AuthMethod.PEM_STORED

    def __repr__(self) -> str:
        """Redacted repr, so a stray log line or traceback cannot leak a secret."""
        fields = [
            f"{name}=<set>"
            for name, value in (
                ("password", self.password),
                ("key_passphrase", self.key_passphrase),
                ("private_key_data", self.private_key_data),
            )
            if value
        ]
        return f"Credential({', '.join(fields) if fields else 'empty'})"
