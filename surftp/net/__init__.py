"""Network transports: the connection layer behind a remote pane.

``SSHSession`` owns the single authenticated connection; ``SFTPFileSystem`` and
``FTPFileSystem`` are ``FileSystem`` backends the panes render without knowing
which protocol produced the rows.
"""

from __future__ import annotations

from surftp.net.ftp import FTPFileSystem
from surftp.net.scp import ScpTransfer
from surftp.net.sftp import SFTPFileSystem
from surftp.net.ssh import PrivateKeyError, SSHSession, check_key_permissions, is_key_encrypted
from surftp.net.types import (
    AuthMethod,
    ConnectionProfile,
    Credential,
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
    "FTPFileSystem",
    "HostKeyChanged",
    "HostKeyUnknown",
    "NetworkError",
    "PrivateKeyError",
    "Protocol",
    "SFTPFileSystem",
    "ScpTransfer",
    "SSHSession",
    "SecretKind",
    "check_key_permissions",
    "is_key_encrypted",
]
