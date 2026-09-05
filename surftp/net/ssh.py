"""The authenticated SSH connection, and the auth material that opens it.

One :class:`SSHSession` owns exactly one ``asyncssh`` connection. SFTP, SCP,
the interactive shell, and port forwards are all **channels multiplexed on it**
— the user authenticates once, and every subsequent feature opens a channel
rather than a second connection. That property is why this class exists at all
instead of each backend connecting for itself.

An SSH *profile* (``Protocol.SSH``) is this connection and nothing more: no
file pane rides on it. It exists for the terminal, for running commands, and
for tunnels, and it is acquired and reference-counted through
``SSHConnectionManager`` (see ``manager.py``).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING, Any

import asyncssh

from surftp.net import hostkeys
from surftp.net.errors import describe_connect_failure
from surftp.net.types import (
    AuthMethod,
    ConnectionProfile,
    Credential,
    ForwardKind,
    ForwardSpec,
    NetworkError,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from surftp.net.sftp import SFTPFileSystem

# How long to wait for a connection before giving up. Without the keepalive a
# half-dead connection hangs a pane forever instead of surfacing an error.
CONNECT_TIMEOUT_SECONDS: int = 15
DEFAULT_KEEPALIVE_INTERVAL_SECONDS: int = 30
KEEPALIVE_MAX_MISSED: int = 3


class PrivateKeyError(NetworkError):
    """A private key could not be loaded — missing, malformed, or wrong passphrase."""


class ForwardError(NetworkError):
    """A port forward could not be started; carries the offending spec."""

    def __init__(self, spec: ForwardSpec, reason: str) -> None:
        """Name the forward that failed and why, per the plan's requirement."""
        super().__init__(f"Forward {spec.description} failed: {reason}")
        self.spec = spec
        self.reason = reason


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


def check_identity_files(identity_files: tuple[str, ...]) -> list[str]:
    """Collect permission warnings for every identity file, not just ``pem_path``.

    Multi-key authentication means each file gets its own check, reusing the
    single-key warning. Warnings, never refusals.
    """
    return [warning for path in identity_files if (warning := check_key_permissions(path))]


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


def connection_key(profile: ConnectionProfile) -> tuple[Any, ...]:
    """The sharing identity of a connection — never the profile name.

    Two profiles pointing at one server must be able to share a connection, and
    two names for one server must not; the key is therefore the addressable
    facts asyncssh itself cares about. ``identity_files`` is included so that
    a key-auth profile and a password-auth profile to the same host do not
    collide.

    ``protocol`` is included even though the plan's list omits it, and that
    omission would be a bug: an SFTP profile (``dedicated_connection=False``)
    and an SSH profile (``dedicated_connection=True``) to the same host would
    otherwise collide in the manager's registry — the dedicated connection
    would overwrite the shared one's entry, orphaning it and leaking it. They
    must be independent connections: that independence is the whole reason the
    dedicated connection exists. Same-protocol profiles still share freely.
    """
    return (
        profile.protocol,
        profile.host,
        profile.port,
        profile.username,
        profile.auth_method,
        profile.identity_files or ((profile.pem_path,) if profile.pem_path else ()),
    )


class ForwardHandle:
    """One running (or failed) port forward on a session.

    A forward is a listener object; it can fail to start (port already bound
    locally, server refusing a remote bind) and that failure must name which
    forward and why without aborting the connection it rides on.
    """

    def __init__(self, spec: ForwardSpec, listener: asyncssh.SSHListener | None) -> None:
        """Wrap a started listener, or ``None`` for a forward that failed."""
        self.spec = spec
        self._listener = listener
        self._state = "running" if listener is not None else "stopped"
        self._error: str | None = None

    @property
    def state(self) -> str:
        """``running``, ``stopped``, or ``failed``.

        Tracked here rather than queried from the listener: asyncssh's
        ``SSHForwardListener`` exposes ``wait_closed`` but no ``is_closed``,
        and asking a stopped listener which of its methods to call is exactly
        the kind of coupling this class exists to avoid.
        """
        return self._state

    @property
    def error(self) -> str | None:
        """Why the forward failed, when it did."""
        return self._error

    @property
    def bound_port(self) -> int | None:
        """The actual bound port; useful when the spec asked for port 0."""
        try:
            if self._listener is not None:
                return self._listener.get_port()
        except Exception:
            pass
        return None

    def mark_failed(self, reason: str) -> None:
        """Record a start failure so the UI can name it."""
        self._state = "failed"
        self._error = reason

    def stop(self) -> None:
        """Close the listener and mark the forward stopped."""
        if self._listener is not None:
            try:
                self._listener.close()
            except Exception:
                pass
            self._listener = None
        if self._state != "failed":
            self._state = "stopped"


class SSHSession:
    """One authenticated SSH connection. Shells, SFTP and tunnels are channels on it."""

    def __init__(self, connection: asyncssh.SSHClientConnection, profile: ConnectionProfile) -> None:
        """Wrap an established connection. Use :meth:`connect` rather than calling this."""
        self._connection = connection
        self._profile = profile
        self._connected_at = time.monotonic()
        self._forwards: dict[int, ForwardHandle] = {}

    @classmethod
    async def connect(
        cls,
        profile: ConnectionProfile,
        credential: Credential,
        *,
        tunnel: asyncssh.SSHClientConnection | None = None,
    ) -> SSHSession:
        """Authenticate to ``profile``'s host and return the live session.

        Host keys are always verified against ``known_hosts``; a failure is
        re-raised as ``HostKeyUnknown`` (ask the user) or ``HostKeyChanged``
        (hard stop) so the UI can tell the two apart. A ``tunnel`` (the jump
        host's own connection) is passed through to asyncssh's ``ProxyJump``;
        it too was verified when it was opened, so a jump never skips host-key
        verification.
        """
        options: dict[str, Any] = {
            "username": profile.username,
            "known_hosts": str(hostkeys.known_hosts_path()),
            "connect_timeout": CONNECT_TIMEOUT_SECONDS,
            "keepalive_interval": profile.keepalive_interval,
            "keepalive_count_max": KEEPALIVE_MAX_MISSED,
            "agent_forwarding": profile.agent_forwarding,
        }
        if profile.compression:
            options["compression_algs"] = ["zlib@openssh.com", "zlib", "none"]
        if tunnel is not None:
            options["tunnel"] = tunnel

        match profile.auth_method:
            case AuthMethod.PASSWORD:
                options["password"] = credential.password or ""
                options["client_keys"] = None  # don't silently fall back to ~/.ssh keys
            case AuthMethod.PEM_FILE:
                keys = [
                    load_private_key(path, None, credential.passphrase_for(i))
                    for i, path in enumerate(profile.key_paths)
                ]
                if not keys:
                    raise PrivateKeyError("No private key was provided for key authentication.")
                options["client_keys"] = keys
            case AuthMethod.PEM_STORED:
                key = load_private_key(
                    profile.pem_path,
                    credential.private_key_data,
                    credential.passphrase_for(0),
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
        """The raw asyncssh connection, for SCP and channel management."""
        return self._connection

    @property
    def is_connected(self) -> bool:
        """Whether the underlying transport is still up."""
        return not self._connection.is_closed()

    @property
    def uptime_seconds(self) -> float:
        """Seconds since this connection was established."""
        return time.monotonic() - self._connected_at

    @property
    def channel_count(self) -> int:
        """Number of open channels multiplexed on this connection.

        Counts the asyncssh connection's live channels, so an SFTP subsystem,
        a shell and three forwards on one session show up as what they are.
        """
        channels = getattr(self._connection, "_channels", None)
        if channels is None:
            return 0
        return len(channels)

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

    async def run_command(self, command: str, timeout: float = 60.0) -> tuple[int | None, str, str]:
        """Run one command without a PTY and return ``(exit_status, stdout, stderr)``.

        Distinct from the interactive shell: a non-interactive command needs no
        terminal emulation, and its output is worth keeping rather than
        scrolling away. The exit status is the whole point — a script that
        fails loudly has failed.
        """
        process = await self._connection.create_process(
            command=command, encoding="utf-8", request_pty=False
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout=timeout)
        finally:
            try:
                process.close()
            except Exception:
                pass
        status = process.returncode
        return (status, out or "", err or "")

    # ------------------------------------------------------------------
    # Port forwarding
    # ------------------------------------------------------------------

    async def start_forward(self, spec: ForwardSpec) -> ForwardHandle:
        """Start one port forward, returning a handle that names a failure instead of raising.

        A forward that fails to bind reports the port and the reason and must
        not abort the connection it rides on, so this never raises for a bind
        failure — it returns a ``ForwardHandle`` in the ``failed`` state.
        """
        handle = ForwardHandle(spec, None)
        try:
            if spec.kind is ForwardKind.LOCAL:
                listener = await self._connection.forward_local_port(
                    spec.listen_host, spec.listen_port, spec.dest_host or "", spec.dest_port or 0
                )
            elif spec.kind is ForwardKind.REMOTE:
                listener = await self._connection.forward_remote_port(
                    spec.listen_host, spec.listen_port, spec.dest_host or "", spec.dest_port or 0
                )
            elif spec.kind is ForwardKind.DYNAMIC:
                listener = await self._connection.forward_socks(spec.listen_host, spec.listen_port)
            elif spec.kind is ForwardKind.SOCKET:
                listener = await self._connection.forward_local_path(
                    spec.listen_path or "", spec.dest_path or ""
                )
            else:
                raise NetworkError(f"Unsupported forward kind: {spec.kind}")
        except (OSError, asyncssh.Error, NetworkError) as exc:
            handle.mark_failed(str(exc))
            return handle

        handle._listener = listener
        handle._state = "running"
        if spec.id is not None:
            self._forwards[spec.id] = handle
        return handle

    def stop_forward(self, forward_id: int) -> None:
        """Stop a running forward by its spec id, if present."""
        handle = self._forwards.pop(forward_id, None)
        if handle is not None:
            handle.stop()

    def forwards(self) -> list[ForwardHandle]:
        """Every forward currently tracked on this session, by spec id."""
        return list(self._forwards.values())

    async def start_auto_forwards(self, specs: list[ForwardSpec]) -> list[ForwardHandle]:
        """Bring up every ``auto_start`` forward after authentication.

        Failures are collected into the handles, not raised: one refused bind
        must not take down the connection it was meant to ride on.
        """
        handles: list[ForwardHandle] = []
        for spec in specs:
            if not spec.auto_start:
                continue
            handle = await self.start_forward(spec)
            handles.append(handle)
        return handles

    async def close(self) -> None:
        """Close the connection, any forwards, and wait for the transport to finish."""
        for handle in list(self._forwards.values()):
            handle.stop()
        self._forwards.clear()
        self._connection.close()
        await self._connection.wait_closed()