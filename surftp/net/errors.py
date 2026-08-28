"""Mapping library exceptions to messages that name the actual cause.

Wrong password, wrong key passphrase, unknown host, changed host key, DNS
failure, refused connection and timeout are each a *different* message. A
single "connection failed" is the failure mode that makes a client like this
unusable, and every one of these is a normal Tuesday.
"""

from __future__ import annotations

import socket

import asyncssh

from surftp.net.types import ConnectionProfile, NetworkError


def describe_connect_failure(exc: BaseException, profile: ConnectionProfile) -> NetworkError:
    """Translate a connection exception into a ``NetworkError`` a user can act on."""
    target = f"{profile.host}:{profile.port}"

    if isinstance(exc, asyncssh.PermissionDenied):
        return NetworkError(_auth_hint(profile))
    if isinstance(exc, asyncssh.KeyEncryptionError):
        return NetworkError(f"Wrong passphrase for the private key {profile.pem_path}.")
    if isinstance(exc, asyncssh.KeyImportError):
        return NetworkError(
            f"Could not read the private key {profile.pem_path}: {exc}. "
            "The file is malformed or not a supported key format."
        )
    if isinstance(exc, socket.gaierror):
        return NetworkError(f"Cannot resolve host name '{profile.host}'.")
    if isinstance(exc, ConnectionRefusedError):
        return NetworkError(f"Connection refused by {target} — is the service listening on that port?")
    if isinstance(exc, (TimeoutError, asyncssh.TimeoutError)):
        return NetworkError(f"Timed out connecting to {target}.")
    if isinstance(exc, asyncssh.DisconnectError):
        return NetworkError(f"{target} disconnected: {exc.reason}")
    if isinstance(exc, OSError):
        return NetworkError(f"Cannot reach {target}: {exc.strerror or exc}")
    return NetworkError(f"Connection to {target} failed: {exc}")


def _auth_hint(profile: ConnectionProfile) -> str:
    """Phrase an auth rejection in terms of the method the user actually chose.

    "Authentication failed" after a mistyped key passphrase is the single most
    confusing failure in a client like this, so the message names the credential
    that was refused.
    """
    from surftp.net.types import AuthMethod

    who = f"{profile.username}@{profile.host}"
    match profile.auth_method:
        case AuthMethod.PASSWORD:
            return f"{who} rejected the password."
        case AuthMethod.PEM_FILE | AuthMethod.PEM_STORED:
            return (
                f"{who} rejected the private key. The key loaded correctly, so this is the server "
                "refusing it — check that its public half is in the account's authorized_keys."
            )
        case AuthMethod.AGENT:
            return f"{who} rejected every key offered by ssh-agent (is the agent running and loaded?)."
    return f"{who} rejected the credentials."
