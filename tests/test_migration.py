"""Migration tests: a v1 vault upgrades to v2 without touching secrets.

This is the riskiest part of the plan — the vault's contents are unrecoverable
if damaged — so the migration has its own suite, per §9:

(a) every secret still decrypts with the original master password,
(b) old profiles keep their values,
(c) the ``.v1.bak`` file exists,
(d) migrating a **locked** vault works (no master password required),
(e) an interrupted migration leaves a readable v1 vault.

The v1 vault is built by hand (v1 DDL + a secret encrypted with the real
crypto layer) because this build no longer writes v1 files.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/home/salman/Documents/surftp")

import duckdb  # noqa: E402

from surftp.net.types import SecretKind  # noqa: E402
from surftp.store import Vault, VaultLockedError  # noqa: E402
from surftp.store import crypto  # noqa: E402
from surftp.store.schema import SCHEMA_VERSION, read_schema_version  # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="surftp-migration-"))
failures = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record one assertion without aborting the rest of the run."""
    global failures
    if condition:
        print(f"  PASS  {label}")
    else:
        failures += 1
        print(f"  FAIL  {label}  {detail}")


V1_DDL = """
CREATE TABLE vault_meta (
    schema_version  INTEGER   NOT NULL,
    kdf_salt        BLOB      NOT NULL,
    kdf_params      VARCHAR   NOT NULL,
    wrapped_dek     BLOB      NOT NULL,
    created_at      TIMESTAMP NOT NULL
);
CREATE SEQUENCE profile_ids START 1;
CREATE TABLE profiles (
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
CREATE TABLE secrets (
    profile_id  UBIGINT NOT NULL,
    kind        VARCHAR NOT NULL,
    ciphertext  BLOB    NOT NULL,
    PRIMARY KEY (profile_id, kind)
);
"""

MASTER = "master-password"
PASSWORD = "hunter2"
PASSPHRASE = "key-pass"


def build_v1(path: Path) -> tuple[int, bytes]:
    """Create a genuine v1 vault at ``path`` with one profile and two secrets.

    Encrypts with the real crypto layer so the migration test proves the
    *ciphertext* survives, not just that the columns line up. Returns the
    profile id and the raw password ciphertext it wrote.
    """
    conn = duckdb.connect(str(path))
    conn.execute(V1_DDL)
    salt = crypto.generate_salt()
    params = crypto.default_kdf_params()
    kek = crypto.derive_kek(MASTER, salt, params)
    dek = crypto.generate_dek()
    conn.execute(
        "INSERT INTO vault_meta VALUES (?, ?, ?, ?, ?)",
        [1, salt, crypto.encode_kdf_params(params), crypto.wrap_dek(kek, dek), datetime.now(timezone.utc)],
    )
    row = conn.execute(
        "INSERT INTO profiles (name, protocol, host, port, username, auth_method, pem_path, "
        "remote_path, use_tls, created_at) "
        "VALUES ('prod', 'sftp', 'prod.example', 22, 'deploy', 'pem_file', '/keys/prod.pem', "
        "'/srv/www', TRUE, ?) RETURNING id",
        [datetime.now(timezone.utc)],
    ).fetchone()
    assert row is not None
    pid = int(row[0])
    password_blob = crypto.encrypt_secret(dek, PASSWORD, pid, "password")
    conn.execute("INSERT INTO secrets VALUES (?, ?, ?)", [pid, "password", password_blob])
    conn.execute(
        "INSERT INTO secrets VALUES (?, ?, ?)",
        [pid, "key_passphrase", crypto.encrypt_secret(dek, PASSPHRASE, pid, "key_passphrase")],
    )
    conn.close()
    return pid, password_blob


print("\n[1] building a v1 vault (the fixture)")
path = WORK / "vault.duckdb"
pid, original_ciphertext = build_v1(path)
check("v1 vault has version 1", read_schema_version(duckdb.connect(str(path))) == 1)

print("\n[2] migration (highest priority)")
vault = Vault.open(path)  # migrates while LOCKED
check("migrated version is 2", read_schema_version(vault._conn) == SCHEMA_VERSION)
check("backup file exists", (WORK / "vault.duckdb.v1.bak").exists())
backup = duckdb.connect(str(WORK / "vault.duckdb.v1.bak"))
check("backup is still v1", read_schema_version(backup) == 1)
backup.close()

profiles = vault.list_profiles()
check("old profile kept", len(profiles) == 1 and profiles[0].name == "prod")
prof = profiles[0]
check("protocol kept", prof.protocol.value == "sftp")
check("host kept", prof.host == "prod.example")
check("username kept", prof.username == "deploy")
check("pem_path kept", prof.pem_path == "/keys/prod.pem")
check("remote_path kept", prof.remote_path == "/srv/www")
check("use_tls kept", prof.use_tls is True)
check("new fields defaulted", prof.identity_files == () and prof.jump_host is None)
check(
    "SFTP profile keeps sharing after migration",
    prof.dedicated_connection is False,
)

# Migration must work on a LOCKED vault: no unlock happened above, and the
# secrets were never touched (no re-encryption). Prove it by unlocking with the
# original master password and reading every secret.
vault.unlock(MASTER)
check("password secret still decrypts", vault.get_secret(pid, SecretKind.PASSWORD) == PASSWORD)
check("passphrase secret still decrypts", vault.get_secret(pid, SecretKind.KEY_PASSPHRASE) == PASSPHRASE)
check(
    "ciphertext unchanged (no re-encryption)",
    vault._conn.execute(
        "SELECT ciphertext FROM secrets WHERE profile_id = ? AND kind = 'password'", [pid]
    ).fetchone()[0]
    == original_ciphertext,
)

print("\n[3] migration is idempotent (reopening a v2 vault does nothing)")
vault.close()
vault = Vault.open(path)
check("reopen keeps version 2", read_schema_version(vault._conn) == SCHEMA_VERSION)
vault.unlock(MASTER)
check("secret still decrypts after reopen", vault.get_secret(pid, SecretKind.PASSWORD) == PASSWORD)
check("backup not clobbered", (WORK / "vault.duckdb.v1.bak").exists())

print("\n[4] interrupted migration leaves a readable v1 vault")
crash = WORK / "crash.duckdb"
build_v1(crash)  # a pristine v1 vault
raw = duckdb.connect(str(crash))
raw.execute("BEGIN")
raw.execute("ALTER TABLE profiles ADD COLUMN compression BOOLEAN DEFAULT FALSE")
# Simulate a crash: close without committing.
raw.close()
reopened = duckdb.connect(str(crash))
check("crash leaves version 1", read_schema_version(reopened) == 1)
check("crash left profiles intact", reopened.execute("SELECT count(*) FROM profiles").fetchone()[0] == 1)
reopened.close()
# And it can still be migrated cleanly afterwards.
vault = Vault.open(crash)
check("recovery migration succeeds", read_schema_version(vault._conn) == SCHEMA_VERSION)
vault.unlock(MASTER)
check("secret survives the interrupted attempt", vault.get_secret(pid, SecretKind.PASSWORD) == PASSWORD)
vault.close()

print("\n[5] a half-migrated vault (column added, version not bumped) completes")
half = WORK / "half.duckdb"
build_v1(half)
raw = duckdb.connect(str(half))
raw.execute("ALTER TABLE profiles ADD COLUMN compression BOOLEAN DEFAULT FALSE")
raw.close()
vault = Vault.open(half)
check("half-migrated vault completes", read_schema_version(vault._conn) == SCHEMA_VERSION)
vault.unlock(MASTER)
check("secret survives partial migration", vault.get_secret(pid, SecretKind.PASSWORD) == PASSWORD)
vault.close()

print("\n[6] a fresh v2 vault is created at the current version")
fresh = WORK / "fresh.duckdb"
fresh_vault = Vault.create(fresh, "new-master")
check("fresh vault is v2", read_schema_version(fresh_vault._conn) == SCHEMA_VERSION)
check("no backup for a fresh vault", not (WORK / "fresh.duckdb.v1.bak").exists())
fresh_vault.close()

print(f"\n{'ALL MIGRATION CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)