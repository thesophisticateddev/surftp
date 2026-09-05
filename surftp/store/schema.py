"""DuckDB DDL for the credential vault, the schema-version guard, and migrations.

Only the ``secrets`` table holds encrypted data. Hostnames, usernames and
profile names stay queryable in the clear so the picker can render its list
without an unlock. That is a deliberate trade: **the vault protects secrets,
not the fact that you have an account on a host.** Anyone who wants the latter
should encrypt the whole database file (see the note in ``vault.py``).
"""

from __future__ import annotations

from pathlib import Path
from shutil import copy2
from typing import Final

import duckdb

SCHEMA_VERSION: Final[int] = 2

DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS vault_meta (
    schema_version  INTEGER   NOT NULL,
    kdf_salt        BLOB      NOT NULL,
    kdf_params      VARCHAR   NOT NULL,
    wrapped_dek     BLOB      NOT NULL,
    created_at      TIMESTAMP NOT NULL
);

CREATE SEQUENCE IF NOT EXISTS profile_ids START 1;

CREATE TABLE IF NOT EXISTS profiles (
    id                    UBIGINT   PRIMARY KEY DEFAULT nextval('profile_ids'),
    name                  VARCHAR   NOT NULL UNIQUE,
    protocol              VARCHAR   NOT NULL,
    host                  VARCHAR   NOT NULL,
    port                  INTEGER   NOT NULL,
    username              VARCHAR   NOT NULL,
    auth_method           VARCHAR   NOT NULL,
    pem_path              VARCHAR,
    remote_path           VARCHAR,
    use_tls               BOOLEAN   NOT NULL DEFAULT FALSE,
    identity_files        VARCHAR,            -- \x1f-joined ordered paths
    jump_host             VARCHAR,
    remote_command        VARCHAR,
    keepalive_interval    INTEGER   NOT NULL DEFAULT 30,
    compression           BOOLEAN   NOT NULL DEFAULT FALSE,
    agent_forwarding      BOOLEAN   NOT NULL DEFAULT FALSE,
    request_pty           BOOLEAN   NOT NULL DEFAULT TRUE,
    dedicated_connection  BOOLEAN   NOT NULL DEFAULT FALSE,
    created_at            TIMESTAMP NOT NULL,
    last_used_at          TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS forward_ids START 1;

CREATE TABLE IF NOT EXISTS forwards (
    id          UBIGINT   PRIMARY KEY DEFAULT nextval('forward_ids'),
    profile_id  UBIGINT   NOT NULL,
    kind        VARCHAR   NOT NULL,   -- 'local' | 'remote' | 'dynamic' | 'socket'
    listen_host VARCHAR   NOT NULL,   -- default '127.0.0.1'; see the security notes
    listen_port INTEGER   NOT NULL,
    dest_host   VARCHAR,              -- NULL for 'dynamic'
    dest_port   INTEGER,              -- NULL for 'dynamic'
    listen_path VARCHAR,              -- kind == 'socket' (local unix socket path)
    dest_path   VARCHAR,              -- kind == 'socket' (remote unix socket path)
    auto_start  BOOLEAN   NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS secrets (
    profile_id  UBIGINT NOT NULL,
    kind        VARCHAR NOT NULL,
    ciphertext  BLOB    NOT NULL,
    PRIMARY KEY (profile_id, kind)
);
"""


def create_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create every table and sequence if it does not already exist."""
    conn.execute(DDL)


def read_schema_version(conn: duckdb.DuckDBPyConnection) -> int | None:
    """Return the stored schema version, or ``None`` for a vault with no meta row."""
    row = conn.execute("SELECT schema_version FROM vault_meta").fetchone()
    return int(row[0]) if row else None


def check_schema_version(conn: duckdb.DuckDBPyConnection) -> None:
    """Refuse to open a vault written by a newer SURFTP.

    Opening a future schema read-write risks silently dropping columns this
    build does not know about, which for a credential store means losing
    secrets. Failing loudly is the only safe option.
    """
    version = read_schema_version(conn)
    if version is None:
        return
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Vault schema v{version} was written by a newer SURFTP "
            f"(this build understands v{SCHEMA_VERSION}). Upgrade SURFTP to open it."
        )


def migrate(conn: duckdb.DuckDBPyConnection, path: Path) -> None:
    """Bring an older vault up to ``SCHEMA_VERSION``, or do nothing.

    Every migration must follow the same rules (see the plan):

    1. **Backup first.** The vault file is copied to ``vault.duckdb.v1.bak``
       before any statement runs, and that fact is surfaced in the UI. There
       is no recovery from a damaged vault — no backup of a master password —
       so the one-file copy is the only safety net and it is mandatory.
    2. **Additive only.** Columns are added, never dropped or renamed; a v1
       build can still read what it understands.
    3. **One transaction**, with ``vault_meta.schema_version`` written *last*.
       A crash mid-migration leaves a v1 vault, not a half-v2 one.
    4. **Secrets are never touched.** No re-encryption and no unlock: the
       ciphertexts and their associated data are unchanged, so migration works
       on a **locked** vault. Requiring the master password to upgrade would
       trap a user who just wanted to open the app.
    """
    version = read_schema_version(conn)
    if version is None or version == SCHEMA_VERSION:
        return
    if version < SCHEMA_VERSION:
        _migrate_1_to_2(conn, path)


def _migrate_1_to_2(conn: duckdb.DuckDBPyConnection, path: Path) -> None:
    """Add the SSH-as-a-protocol columns and the ``forwards`` table.

    Only 1 -> 2 exists today; a chain of migrations would each be its own
    function, and ``migrate`` would walk them in order.

    **One-transaction deviation, recorded honestly.** DuckDB cannot execute two
    ``ALTER TABLE ... ADD COLUMN`` statements on the same table inside one
    transaction (commit fails with "another transaction has altered this
    table"), nor combine them into a single ALTER. So the column additions each
    run standalone — they are additive and idempotent, and a v1 build reading
    a vault with extra columns still works because it selects explicit columns.
    The plan's real guarantee is preserved: the ``forwards`` table and the
    ``schema_version`` bump are committed **together, last**, in one
    transaction, so a crash before that point leaves a vault that still reads
    as v1, and a crash after it leaves a complete v2.
    """
    backup = _backup_path(path)
    if not backup.exists():
        copy2(path, backup)

    # SFTP/SCP/FTP profiles keep sharing a connection, so the migrated default
    # is FALSE; new SSH profiles pass TRUE explicitly.
    _add_column_if_missing(conn, "profiles", "identity_files", "VARCHAR")
    _add_column_if_missing(conn, "profiles", "jump_host", "VARCHAR")
    _add_column_if_missing(conn, "profiles", "remote_command", "VARCHAR")
    _add_column_if_missing(conn, "profiles", "keepalive_interval", "INTEGER DEFAULT 30")
    _add_column_if_missing(conn, "profiles", "compression", "BOOLEAN DEFAULT FALSE")
    _add_column_if_missing(conn, "profiles", "agent_forwarding", "BOOLEAN DEFAULT FALSE")
    _add_column_if_missing(conn, "profiles", "request_pty", "BOOLEAN DEFAULT TRUE")
    _add_column_if_missing(
        conn, "profiles", "dedicated_connection", "BOOLEAN DEFAULT FALSE"
    )

    conn.execute("BEGIN")
    try:
        # The forwards table is created by create_schema for fresh vaults;
        # make sure a vault that predates it gains it too, then write the
        # version row *last* so a crash mid-migration leaves a v1 vault.
        conn.execute("CREATE SEQUENCE IF NOT EXISTS forward_ids START 1")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS forwards (
                id          UBIGINT   PRIMARY KEY DEFAULT nextval('forward_ids'),
                profile_id  UBIGINT   NOT NULL,
                kind        VARCHAR   NOT NULL,
                listen_host VARCHAR   NOT NULL,
                listen_port INTEGER   NOT NULL,
                dest_host   VARCHAR,
                dest_port   INTEGER,
                listen_path VARCHAR,
                dest_path   VARCHAR,
                auto_start  BOOLEAN   NOT NULL DEFAULT TRUE
            )
            """
        )
        conn.execute("UPDATE vault_meta SET schema_version = ?", [SCHEMA_VERSION])
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _add_column_if_missing(
    conn: duckdb.DuckDBPyConnection, table: str, column: str, definition: str
) -> None:
    """Add one column, skipping it when it already exists.

    Idempotence matters here: a vault that crashed *after* a column was added
    but before the version bump must still migrate cleanly on the next open.
    """
    row = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = ? AND column_name = ?",
        [table, column],
    ).fetchone()
    if row is None:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _backup_path(path: Path) -> Path:
    """Return where the pre-migration copy lives: ``vault.duckdb.v1.bak``.

    The name names the *source* version, so a second migration (should one
    ever exist) would produce a ``.v2.bak`` rather than overwrite the v1
    safety net.
    """
    return path.with_name(path.name + ".v1.bak")