"""DuckDB DDL for the credential vault, plus the schema-version guard.

Only the ``secrets`` table holds encrypted data. Hostnames, usernames and
profile names stay queryable in the clear so the picker can render its list
without an unlock. That is a deliberate trade: **the vault protects secrets,
not the fact that you have an account on a host.** Anyone who wants the latter
should encrypt the whole database file (see the note in ``vault.py``).
"""

from __future__ import annotations

from typing import Final

import duckdb

SCHEMA_VERSION: Final[int] = 1

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
    id            UBIGINT   PRIMARY KEY DEFAULT nextval('profile_ids'),
    name          VARCHAR   NOT NULL UNIQUE,
    protocol      VARCHAR   NOT NULL,
    host          VARCHAR   NOT NULL,
    port          INTEGER   NOT NULL,
    username      VARCHAR   NOT NULL,
    auth_method   VARCHAR   NOT NULL,
    pem_path      VARCHAR,
    remote_path   VARCHAR,
    use_tls       BOOLEAN   NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMP NOT NULL,
    last_used_at  TIMESTAMP
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
