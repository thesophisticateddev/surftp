"""Network transports: the connection layer behind a remote pane.

``SSHSession`` owns the single authenticated connection; ``SFTPFileSystem`` and
``FTPFileSystem`` are ``FileSystem`` backends the panes render without knowing
which protocol produced the rows. ``SSHConnectionManager`` owns every live
connection and decides when to share one; ``CredentialCache`` holds one-off
secrets in memory for the session only.
"""

from __future__ import annotations

from surftp.net.credential_cache import CredentialCache, get_credential_cache
from surftp.net.ftp import FTPFileSystem
from surftp.net.manager import SSHConnectionManager, get_manager
from surftp.net.scp import ScpTransfer
from surftp.net.sftp import SFTPFileSystem
from surftp.net.ssh import (
    ForwardHandle,
    PrivateKeyError,
    SSHSession,
    check_identity_files,
    check_key_permissions,
    is_key_encrypted,
)
from surftp.net.types import (
    AuthMethod,
    ConnectionProfile,
    Credential,
    ForwardKind,
    ForwardSpec,
    HostKeyChanged,
    HostKeyUnknown,
    NetworkError,
    Protocol,
    SecretKind,
)

__all__ = [
    "AuthMethod",
    "ConnectionProfile",
    "Credential",
    "CredentialCache",
    "FTPFileSystem",
    "ForwardHandle",
    "ForwardKind",
    "ForwardSpec",
    "HostKeyChanged",
    "HostKeyUnknown",
    "NetworkError",
    "PrivateKeyError",
    "Protocol",
    "SFTPFileSystem",
    "SSHConnectionManager",
    "SSHSession",
    "ScpTransfer",
    "SecretKind",
    "check_identity_files",
    "check_key_permissions",
    "get_credential_cache",
    "get_manager",
    "is_key_encrypted",
]
