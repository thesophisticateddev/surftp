"""Envelope encryption for the credential vault.

Key hierarchy::

    master password --Argon2id(salt)--> KEK (never stored)
                                          |
              wrapped_dek = AESGCM(KEK, DEK)  -- stored in vault_meta
                                          |
                        DEK (random, in memory only while unlocked)
                                          |
                secret ciphertext = AESGCM(DEK, plaintext, nonce, aad)

Why two levels rather than encrypting each secret with the password-derived key
directly: changing the master password then re-wraps *one* 32-byte DEK instead
of decrypting and re-encrypting every stored secret.

There is deliberately **no password verifier record**. AES-GCM is
authenticated, so a failed tag check while unwrapping the DEK *is* the
wrong-password signal; storing a verifier hash would only hand an attacker a
second oracle to attack offline.

This module must not import ``duckdb``: keeping the crypto free of storage
concerns is what makes it unit-testable without a database and reviewable on
its own.
"""

from __future__ import annotations

import json
import os
from typing import Final

from argon2.low_level import Type as Argon2Type
from argon2.low_level import hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Argon2id cost parameters. Stored per-vault in vault_meta.kdf_params so these
# can be raised later without orphaning existing vaults.
KDF_TIME_COST: Final[int] = 3
KDF_MEMORY_COST_KIB: Final[int] = 65536  # 64 MiB
KDF_PARALLELISM: Final[int] = 4

KEK_SIZE_BYTES: Final[int] = 32  # AES-256
DEK_SIZE_BYTES: Final[int] = 32
SALT_SIZE_BYTES: Final[int] = 16
NONCE_SIZE_BYTES: Final[int] = 12  # GCM standard nonce length


class VaultLockedError(Exception):
    """Raised when a secret is accessed while the vault is locked, or unlock fails.

    Deliberately the same type for both: a caller that cannot tell "wrong
    password" from "not unlocked yet" still cannot proceed, and collapsing them
    keeps callers from inventing a fallback path for one of the two.
    """


class VaultCorruptError(Exception):
    """Raised when stored ciphertext fails authentication for a reason other than the password.

    A tampered or truncated record — distinct from a wrong master password, so
    the UI can tell the user their data is damaged rather than blaming a typo.
    """


def default_kdf_params() -> dict[str, int]:
    """Return the KDF cost parameters to stamp into a newly created vault."""
    return {
        "time_cost": KDF_TIME_COST,
        "memory_cost": KDF_MEMORY_COST_KIB,
        "parallelism": KDF_PARALLELISM,
    }


def encode_kdf_params(params: dict[str, int]) -> str:
    """Serialise KDF parameters for the ``vault_meta.kdf_params`` column."""
    return json.dumps(params, sort_keys=True)


def decode_kdf_params(raw: str) -> dict[str, int]:
    """Parse KDF parameters previously written by :func:`encode_kdf_params`."""
    return {str(k): int(v) for k, v in json.loads(raw).items()}


def generate_salt() -> bytes:
    """Return a fresh random KDF salt for a new vault."""
    return os.urandom(SALT_SIZE_BYTES)


def generate_dek() -> bytearray:
    """Return a fresh random data-encryption key.

    A ``bytearray`` rather than ``bytes`` so :func:`zeroize` can overwrite it
    in place when the vault locks.
    """
    return bytearray(os.urandom(DEK_SIZE_BYTES))


def derive_kek(master_password: str, salt: bytes, params: dict[str, int] | None = None) -> bytes:
    """Derive the key-encryption key from the master password with Argon2id.

    ``params`` comes from the vault being opened, so a vault created with older
    cost settings still unlocks after the defaults are raised.
    """
    p = params or default_kdf_params()
    return hash_secret_raw(
        secret=master_password.encode("utf-8"),
        salt=salt,
        time_cost=p["time_cost"],
        memory_cost=p["memory_cost"],
        parallelism=p["parallelism"],
        hash_len=KEK_SIZE_BYTES,
        type=Argon2Type.ID,
    )


def wrap_dek(kek: bytes, dek: bytes) -> bytes:
    """Encrypt the DEK under the KEK, returning ``nonce || ciphertext || tag``."""
    nonce = os.urandom(NONCE_SIZE_BYTES)
    return nonce + AESGCM(kek).encrypt(nonce, bytes(dek), b"surftp-dek")


def unwrap_dek(kek: bytes, wrapped: bytes) -> bytearray:
    """Decrypt the stored DEK, or raise :class:`VaultLockedError` on a bad password.

    The GCM tag check is the password check — see the module docstring.
    """
    nonce, ct = wrapped[:NONCE_SIZE_BYTES], wrapped[NONCE_SIZE_BYTES:]
    try:
        return bytearray(AESGCM(kek).decrypt(nonce, ct, b"surftp-dek"))
    except InvalidTag as exc:
        raise VaultLockedError("Incorrect master password.") from exc


def secret_aad(profile_id: int, kind: str) -> bytes:
    """Build the associated data binding a ciphertext to its own row.

    Without this, a ciphertext copied from one profile row to another would
    still decrypt, silently handing the wrong host's password to a connection.
    The binding makes that a decryption failure instead.
    """
    return f"{profile_id}:{kind}".encode("utf-8")


def encrypt_secret(dek: bytes, plaintext: str, profile_id: int, kind: str) -> bytes:
    """Encrypt one secret, returning ``nonce || ciphertext || tag``.

    A fresh random nonce every call — reusing a nonce under one key breaks
    GCM catastrophically, so the nonce must never be derived from the row id
    or a counter.
    """
    nonce = os.urandom(NONCE_SIZE_BYTES)
    ct = AESGCM(bytes(dek)).encrypt(nonce, plaintext.encode("utf-8"), secret_aad(profile_id, kind))
    return nonce + ct


def decrypt_secret(dek: bytes, blob: bytes, profile_id: int, kind: str) -> str:
    """Decrypt one secret written by :func:`encrypt_secret`.

    A tag failure here is not a wrong password (the DEK already authenticated
    at unlock), so it is reported as corruption.
    """
    nonce, ct = blob[:NONCE_SIZE_BYTES], blob[NONCE_SIZE_BYTES:]
    try:
        plain = AESGCM(bytes(dek)).decrypt(nonce, ct, secret_aad(profile_id, kind))
    except InvalidTag as exc:
        raise VaultCorruptError(f"Stored secret for profile {profile_id} ({kind}) failed its integrity check.") from exc
    return plain.decode("utf-8")


def zeroize(buffer: bytearray | None) -> None:
    """Best-effort overwrite of a key buffer when the vault locks.

    Honest limitation: this works only because the DEK is held in a mutable
    ``bytearray``. Python cannot guarantee the same for ``str`` or ``bytes``
    (the master password included) — the interpreter may have copied those
    anywhere, and they are cleared only when garbage collected.
    """
    if buffer is None:
        return
    for i in range(len(buffer)):
        buffer[i] = 0
