"""DuckDB-backed credential vault.

Locked until :meth:`Vault.unlock` succeeds. Profiles are listable while locked
(they hold no secrets); anything touching ``secrets`` raises
:class:`~surftp.store.crypto.VaultLockedError` until the master password has
been supplied.

**There is no recovery path.** A lost master password means a lost vault, by
design: the KEK exists only as a derivation of that password. The UI must say
so at vault-creation time, not bury it in documentation.

**What this does not protect against.** The vault protects secrets *at rest*
against someone who reads the DuckDB file. It does not protect against malware
running as the user while the vault is unlocked, a keylogger capturing the
master password, or memory inspection of the live process. Claiming more than
that would be dishonest.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import platformdirs

from surftp.net.types import AuthMethod, ConnectionProfile, Protocol, SecretKind
from surftp.store import crypto
from surftp.store.crypto import VaultLockedError
from surftp.store.schema import SCHEMA_VERSION, check_schema_version, create_schema


def default_vault_path() -> Path:
    """Return the per-user vault location, creating its parent directory.

    Uses ``platformdirs`` so the file lands in the OS-conventional data
    directory rather than next to the source tree.
    """
    directory = Path(platformdirs.user_data_dir("surftp"))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory / "vault.duckdb"


class Vault:
    """The credential store. Construct via :meth:`create` or :meth:`open`."""

    def __init__(self, connection: duckdb.DuckDBPyConnection, path: Path) -> None:
        """Wrap an already-open DuckDB connection. Prefer :meth:`open`/:meth:`create`."""
        self._conn = connection
        self._path = path
        self._dek: bytearray | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @classmethod
    def create(cls, path: Path, master_password: str) -> Vault:
        """Initialise a new vault at ``path`` and leave it unlocked.

        Generates the KDF salt and a random DEK, wraps the DEK under the
        password-derived KEK, and stores only the wrapped copy.
        """
        if path.exists() and path.stat().st_size > 0:
            raise FileExistsError(f"A vault already exists at {path}.")
        conn = duckdb.connect(str(path))
        create_schema(conn)
        salt = crypto.generate_salt()
        params = crypto.default_kdf_params()
        kek = crypto.derive_kek(master_password, salt, params)
        dek = crypto.generate_dek()
        conn.execute(
            "INSERT INTO vault_meta VALUES (?, ?, ?, ?, ?)",
            [
                SCHEMA_VERSION,
                salt,
                crypto.encode_kdf_params(params),
                crypto.wrap_dek(kek, dek),
                datetime.now(timezone.utc),
            ],
        )
        # The vault holds every credential the user owns: keep it owner-only.
        path.chmod(0o600)
        vault = cls(conn, path)
        vault._dek = dek
        return vault

    @classmethod
    def open(cls, path: Path) -> Vault:
        """Open an existing vault **locked**. Call :meth:`unlock` before reading secrets."""
        if not path.exists():
            raise FileNotFoundError(f"No vault at {path}.")
        conn = duckdb.connect(str(path))
        create_schema(conn)  # tolerate a vault created by an older build
        check_schema_version(conn)
        return cls(conn, path)

    @classmethod
    def open_or_create(cls, path: Path, master_password: str) -> Vault:
        """Open ``path`` and unlock it, creating the vault first if absent."""
        if path.exists() and path.stat().st_size > 0:
            vault = cls.open(path)
            vault.unlock(master_password)
            return vault
        return cls.create(path, master_password)

    @staticmethod
    def exists(path: Path) -> bool:
        """Whether a vault file is present, so the UI knows to prompt vs. offer setup."""
        return path.exists() and path.stat().st_size > 0

    def unlock(self, master_password: str) -> None:
        """Derive the KEK and unwrap the DEK, or raise ``VaultLockedError``.

        The GCM tag check on the wrapped DEK *is* the password check; there is
        no separate verifier to attack.
        """
        row = self._conn.execute("SELECT kdf_salt, kdf_params, wrapped_dek FROM vault_meta").fetchone()
        if row is None:
            raise VaultLockedError("Vault has no metadata row; it was never initialised.")
        salt, params_raw, wrapped = bytes(row[0]), str(row[1]), bytes(row[2])
        kek = crypto.derive_kek(master_password, salt, crypto.decode_kdf_params(params_raw))
        self._dek = crypto.unwrap_dek(kek, wrapped)

    def lock(self) -> None:
        """Zeroize the in-memory DEK. Profiles remain listable; secrets do not."""
        crypto.zeroize(self._dek)
        self._dek = None

    def close(self) -> None:
        """Lock and close the underlying DuckDB connection."""
        self.lock()
        self._conn.close()

    @property
    def is_unlocked(self) -> bool:
        """Whether secrets can currently be read or written."""
        return self._dek is not None

    @property
    def path(self) -> Path:
        """Filesystem location of this vault, for display in the UI."""
        return self._path

    def _require_dek(self) -> bytearray:
        """Return the DEK or raise; never returns ``None`` for "locked".

        Returning ``None`` would let a caller read it as "no password stored"
        and fall through to a prompt, which is exactly the wrong behaviour.
        """
        if self._dek is None:
            raise VaultLockedError("Vault is locked; unlock it with the master password first.")
        return self._dek

    # ------------------------------------------------------------------
    # Profiles (no unlock required)
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_profile(row: tuple[Any, ...]) -> ConnectionProfile:
        """Build a ``ConnectionProfile`` from a ``profiles`` row."""
        return ConnectionProfile(
            id=int(row[0]),
            name=str(row[1]),
            protocol=Protocol(row[2]),
            host=str(row[3]),
            port=int(row[4]),
            username=str(row[5]),
            auth_method=AuthMethod(row[6]),
            pem_path=row[7],
            remote_path=row[8],
            use_tls=bool(row[9]),
        )

    _PROFILE_COLUMNS = (
        "id, name, protocol, host, port, username, auth_method, pem_path, remote_path, use_tls"
    )

    def list_profiles(self) -> list[ConnectionProfile]:
        """Return every saved profile, most recently used first. No unlock needed."""
        rows = self._conn.execute(
            f"SELECT {self._PROFILE_COLUMNS} FROM profiles "
            "ORDER BY last_used_at DESC NULLS LAST, name"
        ).fetchall()
        return [self._row_to_profile(row) for row in rows]

    def get_profile(self, profile_id: int) -> ConnectionProfile:
        """Return one profile by id, or raise ``KeyError``."""
        row = self._conn.execute(
            f"SELECT {self._PROFILE_COLUMNS} FROM profiles WHERE id = ?", [profile_id]
        ).fetchone()
        if row is None:
            raise KeyError(f"No profile with id {profile_id}.")
        return self._row_to_profile(row)

    def save_profile(self, profile: ConnectionProfile) -> int:
        """Insert or update ``profile`` and return its id.

        Updating by id rather than by name so a rename does not orphan the
        profile's secrets.
        """
        if profile.id is None:
            row = self._conn.execute(
                "INSERT INTO profiles (name, protocol, host, port, username, auth_method, "
                "pem_path, remote_path, use_tls, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                [
                    profile.name,
                    str(profile.protocol),
                    profile.host,
                    profile.port,
                    profile.username,
                    str(profile.auth_method),
                    profile.pem_path,
                    profile.remote_path,
                    profile.use_tls,
                    datetime.now(timezone.utc),
                ],
            ).fetchone()
            assert row is not None
            return int(row[0])

        self._conn.execute(
            "UPDATE profiles SET name = ?, protocol = ?, host = ?, port = ?, username = ?, "
            "auth_method = ?, pem_path = ?, remote_path = ?, use_tls = ? WHERE id = ?",
            [
                profile.name,
                str(profile.protocol),
                profile.host,
                profile.port,
                profile.username,
                str(profile.auth_method),
                profile.pem_path,
                profile.remote_path,
                profile.use_tls,
                profile.id,
            ],
        )
        return profile.id

    def delete_profile(self, profile_id: int) -> None:
        """Delete a profile and every secret belonging to it.

        The secrets go first: a crash between the two statements must not leave
        orphaned ciphertext behind with no profile row to explain it.
        """
        self._conn.execute("DELETE FROM secrets WHERE profile_id = ?", [profile_id])
        self._conn.execute("DELETE FROM profiles WHERE id = ?", [profile_id])

    def touch_profile(self, profile_id: int) -> None:
        """Record a successful connection, so the picker can order by recency."""
        self._conn.execute(
            "UPDATE profiles SET last_used_at = ? WHERE id = ?",
            [datetime.now(timezone.utc), profile_id],
        )

    # ------------------------------------------------------------------
    # Secrets (unlock required)
    # ------------------------------------------------------------------

    def put_secret(self, profile_id: int, kind: SecretKind, value: str) -> None:
        """Encrypt and store one secret, replacing any existing one of that kind."""
        dek = self._require_dek()
        blob = crypto.encrypt_secret(dek, value, profile_id, str(kind))
        self._conn.execute(
            "DELETE FROM secrets WHERE profile_id = ? AND kind = ?", [profile_id, str(kind)]
        )
        self._conn.execute("INSERT INTO secrets VALUES (?, ?, ?)", [profile_id, str(kind), blob])

    def get_secret(self, profile_id: int, kind: SecretKind) -> str | None:
        """Return a decrypted secret, or ``None`` when the profile has none of that kind.

        Raises ``VaultLockedError`` when locked — ``None`` means "not stored",
        and the two must never be conflated.
        """
        dek = self._require_dek()
        row = self._conn.execute(
            "SELECT ciphertext FROM secrets WHERE profile_id = ? AND kind = ?",
            [profile_id, str(kind)],
        ).fetchone()
        if row is None:
            return None
        return crypto.decrypt_secret(dek, bytes(row[0]), profile_id, str(kind))

    def delete_secret(self, profile_id: int, kind: SecretKind) -> None:
        """Remove one stored secret. Allowed while locked — deleting reveals nothing."""
        self._conn.execute(
            "DELETE FROM secrets WHERE profile_id = ? AND kind = ?", [profile_id, str(kind)]
        )

    def change_master_password(self, old_password: str, new_password: str) -> None:
        """Re-wrap the DEK under a key derived from ``new_password``.

        Only the 32-byte DEK is re-encrypted; every stored secret is untouched,
        which is the entire reason for the envelope design. A fresh salt is
        generated so the new password never reuses the old derivation.
        """
        row = self._conn.execute("SELECT kdf_salt, kdf_params, wrapped_dek FROM vault_meta").fetchone()
        if row is None:
            raise VaultLockedError("Vault has no metadata row; it was never initialised.")
        old_salt, params_raw, wrapped = bytes(row[0]), str(row[1]), bytes(row[2])
        old_kek = crypto.derive_kek(old_password, old_salt, crypto.decode_kdf_params(params_raw))
        dek = crypto.unwrap_dek(old_kek, wrapped)  # raises VaultLockedError if `old` is wrong

        new_salt = crypto.generate_salt()
        params = crypto.default_kdf_params()  # take the chance to adopt current costs
        new_kek = crypto.derive_kek(new_password, new_salt, params)
        self._conn.execute(
            "UPDATE vault_meta SET kdf_salt = ?, kdf_params = ?, wrapped_dek = ?",
            [new_salt, crypto.encode_kdf_params(params), crypto.wrap_dek(new_kek, dek)],
        )
        self._dek = dek
