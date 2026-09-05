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

from surftp.net.types import AuthMethod, ConnectionProfile, ForwardKind, ForwardSpec, Protocol, SecretKind
from surftp.store import crypto
from surftp.store.crypto import VaultLockedError
from surftp.store.schema import SCHEMA_VERSION, check_schema_version, create_schema, migrate

# How ordered identity-file paths are packed into one VARCHAR column. The unit
# separator is a control byte that cannot realistically appear in a key path,
# so it cannot be confused with a separator the way a comma could be.
_IDENTITY_SEP: str = "\x1f"


def _encode_identity_files(paths: tuple[str, ...]) -> str | None:
    """Serialise ordered identity-file paths for the profiles column."""
    return _IDENTITY_SEP.join(paths) if paths else None


def _decode_identity_files(raw: str | None) -> tuple[str, ...]:
    """Parse identity-file paths back out of the profiles column."""
    if not raw:
        return ()
    return tuple(part for part in raw.split(_IDENTITY_SEP) if part)


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
        """Open an existing vault **locked**. Call :meth:`unlock` before reading secrets.

        Older vaults are migrated to the current schema here, before any DDL
        from this build runs, so the pre-migration backup is a faithful copy of
        the file the user actually had. Migration touches no secrets and needs
        no unlock, so opening a locked vault upgrades it quietly.
        """
        if not path.exists():
            raise FileNotFoundError(f"No vault at {path}.")
        conn = duckdb.connect(str(path))
        check_schema_version(conn)  # refuse a *newer* vault before touching it
        migrate(conn, path)  # v1 -> v2: backup first, one transaction, secrets untouched
        create_schema(conn)  # tolerate a vault created by an older build
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
            identity_files=_decode_identity_files(row[10]),
            jump_host=row[11],
            remote_command=row[12],
            keepalive_interval=int(row[13]),
            compression=bool(row[14]),
            agent_forwarding=bool(row[15]),
            request_pty=bool(row[16]),
            dedicated_connection=bool(row[17]),
        )

    _PROFILE_COLUMNS = (
        "id, name, protocol, host, port, username, auth_method, pem_path, remote_path, use_tls, "
        "identity_files, jump_host, remote_command, keepalive_interval, compression, "
        "agent_forwarding, request_pty, dedicated_connection"
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
                "pem_path, remote_path, use_tls, identity_files, jump_host, remote_command, "
                "keepalive_interval, compression, agent_forwarding, request_pty, "
                "dedicated_connection, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
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
                    _encode_identity_files(profile.identity_files),
                    profile.jump_host,
                    profile.remote_command,
                    profile.keepalive_interval,
                    profile.compression,
                    profile.agent_forwarding,
                    profile.request_pty,
                    profile.dedicated_connection,
                    datetime.now(timezone.utc),
                ],
            ).fetchone()
            assert row is not None
            return int(row[0])

        self._conn.execute(
            "UPDATE profiles SET name = ?, protocol = ?, host = ?, port = ?, username = ?, "
            "auth_method = ?, pem_path = ?, remote_path = ?, use_tls = ?, identity_files = ?, "
            "jump_host = ?, remote_command = ?, keepalive_interval = ?, compression = ?, "
            "agent_forwarding = ?, request_pty = ?, dedicated_connection = ? WHERE id = ?",
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
                _encode_identity_files(profile.identity_files),
                profile.jump_host,
                profile.remote_command,
                profile.keepalive_interval,
                profile.compression,
                profile.agent_forwarding,
                profile.request_pty,
                profile.dedicated_connection,
                profile.id,
            ],
        )
        return profile.id

    def delete_profile(self, profile_id: int) -> None:
        """Delete a profile, every secret belonging to it, and its forwards.

        The secrets and forwards go first: a crash between the statements must
        not leave orphaned rows behind with no profile row to explain them.
        """
        self._conn.execute("DELETE FROM forwards WHERE profile_id = ?", [profile_id])
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

    def put_secret(self, profile_id: int, kind: SecretKind | str, value: str) -> None:
        """Encrypt and store one secret, replacing any existing one of that kind.

        ``kind`` may be a ``SecretKind`` or the indexed passphrase form
        ``"key_passphrase:<index>"`` returned by :func:`key_passphrase_kind`,
        which is how several identity files keep separate passphrase records.
        """
        dek = self._require_dek()
        blob = crypto.encrypt_secret(dek, value, profile_id, str(kind))
        self._conn.execute(
            "DELETE FROM secrets WHERE profile_id = ? AND kind = ?", [profile_id, str(kind)]
        )
        self._conn.execute("INSERT INTO secrets VALUES (?, ?, ?)", [profile_id, str(kind), blob])

    def get_secret(self, profile_id: int, kind: SecretKind | str) -> str | None:
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

    def delete_secret(self, profile_id: int, kind: SecretKind | str) -> None:
        """Remove one stored secret. Allowed while locked — deleting reveals nothing."""
        self._conn.execute(
            "DELETE FROM secrets WHERE profile_id = ? AND kind = ?", [profile_id, str(kind)]
        )

    # ------------------------------------------------------------------
    # Forwards (no unlock required — they hold no secrets)
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_forward(row: tuple[Any, ...]) -> ForwardSpec:
        """Build a ``ForwardSpec`` from a ``forwards`` row."""
        return ForwardSpec(
            id=int(row[0]),
            kind=ForwardKind(row[2]),
            listen_host=str(row[3]),
            listen_port=int(row[4]),
            dest_host=row[5],
            dest_port=int(row[6]) if row[6] is not None else None,
            listen_path=row[7],
            dest_path=row[8],
            auto_start=bool(row[9]),
        )

    _FORWARD_COLUMNS = (
        "id, profile_id, kind, listen_host, listen_port, dest_host, dest_port, "
        "listen_path, dest_path, auto_start"
    )

    def list_forwards(self, profile_id: int) -> list[ForwardSpec]:
        """Return every forward saved for one profile, in insertion order."""
        rows = self._conn.execute(
            f"SELECT {self._FORWARD_COLUMNS} FROM forwards WHERE profile_id = ? ORDER BY id",
            [profile_id],
        ).fetchall()
        return [self._row_to_forward(row) for row in rows]

    def get_forward(self, forward_id: int) -> ForwardSpec:
        """Return one forward by id, or raise ``KeyError``."""
        row = self._conn.execute(
            f"SELECT {self._FORWARD_COLUMNS} FROM forwards WHERE id = ?", [forward_id]
        ).fetchone()
        if row is None:
            raise KeyError(f"No forward with id {forward_id}.")
        return self._row_to_forward(row)

    def save_forward(self, profile_id: int, spec: ForwardSpec) -> int:
        """Insert or update a forward for a profile and return its id.

        Updating keeps the row's id so a live forward can be identified while
        its definition is edited.
        """
        if spec.id is None:
            row = self._conn.execute(
                "INSERT INTO forwards (profile_id, kind, listen_host, listen_port, "
                "dest_host, dest_port, listen_path, dest_path, auto_start) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                [
                    profile_id,
                    str(spec.kind),
                    spec.listen_host,
                    spec.listen_port,
                    spec.dest_host,
                    spec.dest_port,
                    spec.listen_path,
                    spec.dest_path,
                    spec.auto_start,
                ],
            ).fetchone()
            assert row is not None
            return int(row[0])

        self._conn.execute(
            "UPDATE forwards SET kind = ?, listen_host = ?, listen_port = ?, dest_host = ?, "
            "dest_port = ?, listen_path = ?, dest_path = ?, auto_start = ? WHERE id = ?",
            [
                str(spec.kind),
                spec.listen_host,
                spec.listen_port,
                spec.dest_host,
                spec.dest_port,
                spec.listen_path,
                spec.dest_path,
                spec.auto_start,
                spec.id,
            ],
        )
        return spec.id

    def delete_forward(self, forward_id: int) -> None:
        """Remove one forward row."""
        self._conn.execute("DELETE FROM forwards WHERE id = ?", [forward_id])

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
