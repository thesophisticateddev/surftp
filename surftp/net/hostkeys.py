"""Host-key trust decisions for SSH connections.

SURFTP reads and writes the user's own ``~/.ssh/known_hosts`` so it inherits
the trust they already established with OpenSSH, and so trusting a host here
also trusts it there.

The rules, which are not negotiable:

* **Unknown host** -> :class:`~surftp.net.types.HostKeyUnknown`, so the UI can
  show the fingerprint and ask. Accepting is an explicit keystroke.
* **Changed key** -> :class:`~surftp.net.types.HostKeyChanged`, a hard failure
  with no "accept anyway" path in the UI.
* ``known_hosts=None`` is never passed to :func:`asyncssh.connect`; that
  disables verification entirely. The one place this module talks to an
  unverified server is :func:`fetch_host_key`, which authenticates nothing and
  sends no credentials — it exists purely to render a fingerprint.
"""

from __future__ import annotations

import os
from pathlib import Path

import asyncssh
from asyncssh.known_hosts import match_known_hosts, read_known_hosts

from surftp.net.types import HostKeyChanged, HostKeyUnknown, NetworkError


def known_hosts_path() -> Path:
    """Return the path to the user's ``known_hosts``, creating it if absent.

    The file itself is created empty, not just its directory: asyncssh opens
    the path it is handed, so a user who has never used SSH before would
    otherwise meet ``FileNotFoundError`` on their very first connection instead
    of the trust prompt. An empty file means "no host is known yet", which is
    exactly the right starting state.
    """
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    hosts_file = ssh_dir / "known_hosts"
    if not hosts_file.exists():
        hosts_file.touch(mode=0o600)
    return hosts_file


def has_known_entry(host: str, port: int, path: Path | None = None) -> bool:
    """Whether ``known_hosts`` holds any key for this host.

    This is what separates "never seen this host" from "the key changed": a
    verification failure with existing entries is a mismatch, and a
    verification failure with none is simply a first connection.
    """
    hosts_file = path or known_hosts_path()
    if not hosts_file.exists():
        return False
    try:
        known = read_known_hosts(str(hosts_file))
        trusted, ca, _revoked, *_rest = match_known_hosts(known, host, host, port)
    except (OSError, ValueError, asyncssh.KeyImportError):
        # An unreadable or malformed known_hosts must not be read as "trusted".
        return False
    return bool(trusted) or bool(ca)


async def fetch_host_key(host: str, port: int) -> asyncssh.SSHKey:
    """Retrieve a server's host key for display in the trust prompt.

    Uses :func:`asyncssh.get_server_host_key`, which completes the key exchange
    and stops *before* authentication — no username, password or key is sent to
    the unverified server. The returned key is only ever shown to the user; it
    is never used to establish a session.
    """
    try:
        key = await asyncssh.get_server_host_key(host, port)
    except (OSError, asyncssh.Error) as exc:
        raise NetworkError(f"Could not reach {host}:{port} to read its host key: {exc}") from exc
    if key is None:
        raise NetworkError(f"{host}:{port} offered no host key.")
    return key


def fingerprint(key: asyncssh.SSHKey) -> str:
    """Return the SHA-256 fingerprint string, in the format OpenSSH prints."""
    return key.get_fingerprint("sha256")


async def classify_failure(host: str, port: int) -> NetworkError:
    """Turn a verification failure into the specific error the UI should show.

    Called after :func:`asyncssh.connect` raises ``HostKeyNotVerifiable``, at
    which point the connection is already torn down and the only question left
    is *why* — a first-time host or a changed key.
    """
    if has_known_entry(host, port):
        return HostKeyChanged(
            f"The host key for {host}:{port} does not match the one in known_hosts. "
            "This is either a rebuilt server or an interception attempt — verify it out of band "
            "and remove the stale line yourself before reconnecting."
        )
    key = await fetch_host_key(host, port)
    return HostKeyUnknown(host, port, fingerprint(key), key.algorithm.decode("ascii", "replace"))


def remember_host_key(host: str, port: int, key: asyncssh.SSHKey, path: Path | None = None) -> None:
    """Append a host key to ``known_hosts`` after the user explicitly accepted it.

    Non-standard ports are written in OpenSSH's ``[host]:port`` form so the
    entry matches on reconnect.
    """
    hosts_file = path or known_hosts_path()
    pattern = host if port == 22 else f"[{host}]:{port}"
    line = f"{pattern} {key.export_public_key('openssh').decode().strip()}\n"
    existing = hosts_file.read_bytes() if hosts_file.exists() else b""
    prefix = b"" if (not existing or existing.endswith(b"\n")) else b"\n"
    with open(hosts_file, "ab") as handle:
        handle.write(prefix + line.encode())
    os.chmod(hosts_file, 0o600)
