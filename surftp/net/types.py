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
    SSH = "ssh"


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


class ForwardKind(StrEnum):
    """The four kinds of port forward an SSH connection can carry.

    ``socket`` is the Unix-socket local forward (``ssh -L`` with a socket
    path), useful for reaching a remote Docker socket.
    """

    LOCAL = "local"
    REMOTE = "remote"
    DYNAMIC = "dynamic"
    SOCKET = "socket"


DEFAULT_PORTS: dict[Protocol, int] = {
    Protocol.SFTP: 22,
    Protocol.SCP: 22,
    Protocol.FTP: 21,
    Protocol.SSH: 22,
}


def key_passphrase_kind(index: int) -> str:
    """Return the vault secret kind for one identity file's passphrase.

    Multiple identity files must not share one passphrase record, so the kind
    is ``key_passphrase:<index>`` mirroring the order of ``identity_files``.
    Index 0 is the same row that the legacy ``SecretKind.KEY_PASSPHRASE``
    writes, so a single-key profile saved before this feature still resolves.
    """
    return f"{SecretKind.KEY_PASSPHRASE}:{index}"


class NetworkError(Exception):
    """The single error type the connection layer raises.

    Every underlying library error (asyncssh, aioftp, socket, DNS) is mapped to
    this at the layer boundary with a message that names the *actual* cause —
    see ``surftp.net.errors``. A bare "connection failed" is the failure mode
    that makes a client like this unusable.
    """
class OpenConnectionError(Exception):
    """This exception is dedicated to opening connection error exceptions 
    The error could originate due to network, io, socket, DNS or 
    any other issues.
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
    # --- SSH-as-a-protocol fields (ignored for FTP; meaningful for SSH/SFTP/SCP)
    identity_files: tuple[str, ...] = ()  # ordered, like repeated ssh -i
    jump_host: str | None = None  # ProxyJump, "user@host:port"
    remote_command: str | None = None  # run instead of a login shell
    keepalive_interval: int = 30
    compression: bool = False
    agent_forwarding: bool = False  # off by default; see the security notes
    request_pty: bool = True
    dedicated_connection: bool = True  # SSH profiles own their connection

    def __post_init__(self) -> None:
        """Fill in the protocol's default port when none was given.

        ``dedicated_connection`` defaults to ``True`` for SSH profiles, which
        is the whole point of the plan; SFTP/SCP/FTP profiles default to
        ``False`` so the historic "one connection, many channels" behaviour is
        preserved exactly. The override cannot live in the field default
        because the default is the same for every profile, so it is applied
        here from the protocol.
        """
        if not self.port:
            object.__setattr__(self, "port", DEFAULT_PORTS[self.protocol])
        if self.protocol is not Protocol.SSH:
            object.__setattr__(self, "dedicated_connection", False)

    @property
    def display(self) -> str:
        """``user@host:port`` — the pane title and the picker's second column."""
        return f"{self.username}@{self.host}:{self.port}"

    @property
    def is_ssh(self) -> bool:
        """Whether this profile is reached over SSH (SFTP and SCP both are)."""
        return self.protocol in (Protocol.SFTP, Protocol.SCP, Protocol.SSH)

    @property
    def key_paths(self) -> tuple[str, ...]:
        """The ordered list of identity-file paths, ``pem_path`` first.

        ``pem_path`` is the single-key form kept for backward compatibility;
        ``identity_files`` is the general form. ``pem_path`` is treated as the
        first element when set, exactly as the plan requires.
        """
        if self.identity_files:
            return self.identity_files
        if self.pem_path:
            return (self.pem_path,)
        return ()

    def with_id(self, profile_id: int) -> ConnectionProfile:
        """Return a copy carrying the id assigned by the vault on save."""
        return replace(self, id=profile_id)


@dataclass(frozen=True, slots=True)
class ForwardSpec:
    """One port forward a profile can carry; rows in the vault's ``forwards`` table.

    ``listen_host`` defaults to ``127.0.0.1`` — a local forward or SOCKS proxy
    bound to ``0.0.0.0`` turns the user's machine into an open relay into the
    remote network, so binding elsewhere is possible but never a default.

    ``socket`` forwards carry the two Unix socket paths instead of
    host/port pairs (``ssh -L /local.sock:/remote.sock``).
    """

    id: int | None = None
    kind: ForwardKind = ForwardKind.LOCAL
    listen_host: str = "127.0.0.1"
    listen_port: int = 0
    dest_host: str | None = None
    dest_port: int | None = None
    listen_path: str | None = None  # kind == SOCKET
    dest_path: str | None = None  # kind == SOCKET
    auto_start: bool = True

    @property
    def description(self) -> str:
        """Render the forward as ``listen -> dest``, the shape ``ssh -L`` shows."""
        if self.kind is ForwardKind.SOCKET:
            return f"{self.listen_path} -> {self.dest_path}"
        if self.kind is ForwardKind.DYNAMIC:
            return f"{self.listen_host}:{self.listen_port} (SOCKS)"
        return f"{self.listen_host}:{self.listen_port} -> {self.dest_host}:{self.dest_port}"


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
    key_passphrases: tuple[str | None, ...] = ()  # per identity file, by index

    def passphrase_for(self, index: int) -> str | None:
        """Return the passphrase for one identity file, falling back to the single form."""
        if index < len(self.key_passphrases) and self.key_passphrases[index] is not None:
            return self.key_passphrases[index]
        if index == 0:
            return self.key_passphrase
        return None

    def __repr__(self) -> str:
        """Redacted repr, so a stray log line or traceback cannot leak a secret."""
        present = [
            name
            for name, value in (
                ("password", self.password),
                ("key_passphrase", self.key_passphrase),
                ("private_key_data", self.private_key_data),
                ("key_passphrases", self.key_passphrases),
            )
            if value
        ]
        fields = [f"{name}=<set>" for name in present] or ["empty"]
        return f"Credential({', '.join(fields)})"
