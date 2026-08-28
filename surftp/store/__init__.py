"""Encrypted local credential store.

``crypto`` holds the key hierarchy and knows nothing about storage; ``vault``
holds the DuckDB access and implements no crypto. Keeping the two apart is what
makes the encryption reviewable and testable on its own.
"""

from __future__ import annotations

from surftp.store.crypto import VaultCorruptError, VaultLockedError
from surftp.store.vault import Vault, default_vault_path

__all__ = ["Vault", "VaultCorruptError", "VaultLockedError", "default_vault_path"]
