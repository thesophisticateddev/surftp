"""The connection manager: owns every live SSH connection and decides when to share one.

Before this module, every ``open_connection`` created its own ``SSHSession``
and closed it when its pane closed. That is exactly right for "one connection,
many channels", but an SSH profile (a terminal or a tunnel with no file pane)
has no pane to own the connection — so something has to own the connections and
reference-count them.

Rules, from the plan:

* **Keyed by** ``(host, port, username, auth_method, identity_files)`` — never
  by profile name, or two profiles pointing at one server would fail to share
  when sharing was wanted.
* **``dedicated_connection`` forces a new connection** even when a matching one
  exists. SSH profiles default to ``True``; SFTP profiles default to ``False``
  so today's "one connection, many channels" behaviour is preserved exactly.
* **Reference counted.** A connection closes when its last consumer releases
  it — a shell tab, an SFTP pane and three forwards can share one session, and
  the connection must outlive the first of them to close. Getting this wrong
  yields either leaked connections or a tunnel that dies when a pane closes.
* **Jump hosts are connections too.** A jump host is acquired through the same
  manager so it can be shared and reference-counted, and its own host key is
  verified — a ``ProxyJump`` that skipped verification would be a hole straight
  through the security model.
* **Registered for shutdown.** :meth:`close_all` runs from the widget that owns
  the sessions (the app), not from an app-level ``on_unmount`` hook — the
  leaked-connection bug in the tabs work proved that ``App.on_unmount`` fires
  after its children are gone.
"""

from __future__ import annotations

from typing import Any

from surftp.net.ssh import SSHSession, connection_key
from surftp.net.types import ConnectionProfile, Credential, NetworkError, Protocol

MAX_CONNECTIONS: int = 8  # the same ceiling as session tabs per side


class _Entry:
    """One live connection and its reference count."""

    __slots__ = ("key", "session", "refcount", "tunnel_key")

    def __init__(self, key: tuple[Any, ...], session: SSHSession) -> None:
        """Hold ``session`` owned by ``key`` with one reference."""
        self.key = key
        self.session = session
        self.refcount = 1
        self.tunnel_key: tuple[Any, ...] | None = None


class SSHConnectionManager:
    """Owns every live SSH connection and decides when to share one."""

    def __init__(self, max_connections: int = MAX_CONNECTIONS) -> None:
        """Create an empty manager with a hard connection ceiling."""
        self._sessions: dict[tuple[Any, ...], _Entry] = {}
        self._max_connections = max_connections

    async def acquire(
        self, profile: ConnectionProfile, credential: Credential
    ) -> SSHSession:
        """Return a connection for ``profile``, sharing one when that is allowed.

        A new connection is opened when none matches, or when the profile
        demands its own (``dedicated_connection``). Either way the caller is
        granted one reference, which :meth:`release` must repay.
        """
        key = connection_key(profile)
        if not profile.dedicated_connection:
            existing = self._sessions.get(key)
            if existing is not None and existing.session.is_connected:
                existing.refcount += 1
                return existing.session

        # A fresh connection is needed. If the profile routes through a jump
        # host, acquire that first — through this same manager, so it is shared
        # and reference-counted and its host key was verified on the way in.
        tunnel_session: SSHSession | None = None
        tunnel_key: tuple[Any, ...] | None = None
        if profile.jump_host:
            jump_profile = self._jump_profile(profile)
            tunnel_session = await self.acquire(jump_profile, credential)
            tunnel_key = connection_key(jump_profile)

        if len(self._sessions) >= self._max_connections:
            if tunnel_session is not None:
                await self.release(tunnel_session)
            raise NetworkError(
                f"Maximum of {self._max_connections} concurrent SSH connections reached. "
                "Disconnect one before opening another."
            )

        try:
            session = await SSHSession.connect(
                profile,
                credential,
                tunnel=tunnel_session.connection if tunnel_session is not None else None,
            )
        except BaseException:
            if tunnel_session is not None:
                await self.release(tunnel_session)
            raise

        entry = _Entry(key, session)
        entry.tunnel_key = tunnel_key
        self._sessions[key] = entry
        return session

    def retain(self, session: SSHSession) -> None:
        """Grant one more reference to a session this manager owns.

        Used when an SSH session opens an SFTP pane on demand: the pane will
        release on close, so it must be given a reference to release.
        """
        for entry in self._sessions.values():
            if entry.session is session:
                entry.refcount += 1
                return
        raise NetworkError("Cannot retain a session this manager does not own.")

    async def release(self, session: SSHSession) -> None:
        """Repay one reference; close the connection when none remain.

        The session's jump host, if any, is released in turn so a dead tunnel
        never outlives the connection that rode through it.
        """
        for key, entry in list(self._sessions.items()):
            if entry.session is not session:
                continue
            entry.refcount -= 1
            if entry.refcount > 0:
                return
            del self._sessions[key]
            await session.close()
            if entry.tunnel_key is not None:
                tunnel = self._sessions.get(entry.tunnel_key)
                if tunnel is not None:
                    await self.release(tunnel.session)
            return

    def sessions(self) -> list[SSHSession]:
        """Every live connection, in no particular order."""
        return [entry.session for entry in self._sessions.values()]

    def is_owned(self, session: SSHSession) -> bool:
        """Whether ``session`` is still alive in this manager (refcount > 0)."""
        return any(entry.session is session for entry in self._sessions.values())

    async def close_all(self) -> None:
        """Close every live connection. Runs from the app at shutdown."""
        for key in list(self._sessions.keys()):
            entry = self._sessions.pop(key)
            await entry.session.close()

    @staticmethod
    def _jump_profile(profile: ConnectionProfile) -> ConnectionProfile:
        """Build the profile for ``profile.jump_host`` ("user@host:port").

        The jump authenticates with the same method, identity files and
        keepalive settings as the target — the common real-world case is agent
        auth (one agent holds both keys), and for password profiles the same
        credential is tried. A jump host that needs different credentials is
        out of scope for a single profile row; use ``~/.ssh/config`` for that.
        """
        raw = profile.jump_host
        assert raw is not None
        user = profile.username
        host = raw
        port = 22
        if "@" in host:
            user, host = host.rsplit("@", 1)
        if host.startswith("["):
            end = host.index("]")
            if host[end : end + 2] == "]:":
                port = int(host[end + 2 :])
            host = host[1:end]
        elif ":" in host:
            host, port_str = host.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                pass
        return ConnectionProfile(
            name=f"jump-{host}",
            protocol=Protocol.SSH,
            host=host,
            port=port,
            username=user,
            auth_method=profile.auth_method,
            pem_path=profile.pem_path,
            identity_files=profile.identity_files,
            keepalive_interval=profile.keepalive_interval,
            compression=profile.compression,
        )


_default_manager: SSHConnectionManager | None = None


def get_manager() -> SSHConnectionManager:
    """Return the process-wide connection manager."""
    global _default_manager
    if _default_manager is None:
        _default_manager = SSHConnectionManager()
    return _default_manager


def reset_manager() -> None:
    """Drop the process-wide manager (used by tests between suites)."""
    global _default_manager
    _default_manager = None


__all__ = [
    "MAX_CONNECTIONS",
    "SSHConnectionManager",
    "get_manager",
    "reset_manager",
]