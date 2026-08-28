"""Unit tests for the crypto layer and the vault. No network, no terminal."""

from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, "/home/salman/Documents/surftp")

from surftp.net.types import AuthMethod, ConnectionProfile, Protocol, SecretKind  # noqa: E402
from surftp.store import Vault, VaultCorruptError, VaultLockedError  # noqa: E402
from surftp.store import crypto  # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="surftp-vault-"))
failures = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record one assertion without aborting the rest of the run."""
    global failures
    if condition:
        print(f"  PASS  {label}")
    else:
        failures += 1
        print(f"  FAIL  {label}  {detail}")


def profile(name: str = "p") -> ConnectionProfile:
    """A minimal saveable profile."""
    return ConnectionProfile(
        name=name, protocol=Protocol.SFTP, host="h", username="u", auth_method=AuthMethod.PASSWORD
    )


print("\n[1] crypto")
salt = crypto.generate_salt()
kek = crypto.derive_kek("master", salt)
dek = crypto.generate_dek()
wrapped = crypto.wrap_dek(kek, dek)
check("dek round trip", bytes(crypto.unwrap_dek(kek, wrapped)) == bytes(dek))
try:
    crypto.unwrap_dek(crypto.derive_kek("wrong", salt), wrapped)
    check("wrong password rejected", False)
except VaultLockedError:
    check("wrong password rejected", True)

blob = crypto.encrypt_secret(dek, "s3cret", 1, "password")
check("secret round trip", crypto.decrypt_secret(dek, blob, 1, "password") == "s3cret")
check("nonce is unique", crypto.encrypt_secret(dek, "x", 1, "p") != crypto.encrypt_secret(dek, "x", 1, "p"))

tampered = bytearray(blob)
tampered[-1] ^= 0x01
try:
    crypto.decrypt_secret(dek, bytes(tampered), 1, "password")
    check("tampered ciphertext rejected", False)
except VaultCorruptError:
    check("tampered ciphertext rejected", True)

try:
    crypto.decrypt_secret(dek, blob, 2, "password")
    check("aad blocks cross-profile reuse", False, "decrypted under the wrong profile id!")
except VaultCorruptError:
    check("aad blocks cross-profile reuse", True)

try:
    crypto.decrypt_secret(dek, blob, 1, "key_passphrase")
    check("aad blocks cross-kind reuse", False)
except VaultCorruptError:
    check("aad blocks cross-kind reuse", True)

buf = bytearray(b"secret")
crypto.zeroize(buf)
check("zeroize clears the buffer", bytes(buf) == b"\0" * 6)

old_params = {"time_cost": 1, "memory_cost": 8192, "parallelism": 1}
check(
    "kdf params round trip",
    crypto.decode_kdf_params(crypto.encode_kdf_params(old_params)) == old_params,
)
check(
    "stored params are honoured",
    crypto.derive_kek("m", salt, old_params) != crypto.derive_kek("m", salt),
)

print("\n[2] vault lifecycle")
path = WORK / "v.duckdb"
vault = Vault.create(path, "master")
check("file is owner-only", oct(path.stat().st_mode & 0o777) == "0o600")
pid = vault.save_profile(profile("prod"))
vault.put_secret(pid, SecretKind.PASSWORD, "p@ss")
vault.put_secret(pid, SecretKind.KEY_PASSPHRASE, "kp")

vault.lock()
check("profiles list while locked", [p.name for p in vault.list_profiles()] == ["prod"])
try:
    vault.get_secret(pid, SecretKind.PASSWORD)
    check("locked vault refuses secrets", False)
except VaultLockedError:
    check("locked vault refuses secrets", True)
try:
    vault.put_secret(pid, SecretKind.PASSWORD, "x")
    check("locked vault refuses writes", False)
except VaultLockedError:
    check("locked vault refuses writes", True)

vault.unlock("master")
check("secret survives lock/unlock", vault.get_secret(pid, SecretKind.PASSWORD) == "p@ss")
check("missing kind returns None", vault.get_secret(pid, SecretKind.PRIVATE_KEY) is None)

print("\n[3] master password rotation")
vault.change_master_password("master", "new-master")
check("every secret survives rotation", vault.get_secret(pid, SecretKind.PASSWORD) == "p@ss")
check("second secret survives too", vault.get_secret(pid, SecretKind.KEY_PASSPHRASE) == "kp")
try:
    vault.change_master_password("master", "x")
    check("rotation requires the current password", False)
except VaultLockedError:
    check("rotation requires the current password", True)
vault.close()

reopened = Vault.open(path)
try:
    reopened.unlock("master")
    check("old password no longer works", False)
except VaultLockedError:
    check("old password no longer works", True)
reopened.unlock("new-master")
check("new password works after reopen", reopened.get_secret(pid, SecretKind.PASSWORD) == "p@ss")

print("\n[4] profile CRUD")
second = reopened.save_profile(profile("staging"))
reopened.touch_profile(second)
check("recency ordering", [p.name for p in reopened.list_profiles()][0] == "staging")
check("get_profile round trip", reopened.get_profile(pid).name == "prod")
renamed = replace(reopened.get_profile(pid), name="prod-renamed")
reopened.save_profile(renamed)
check("update keeps the id", reopened.get_profile(pid).name == "prod-renamed")
check("rename does not orphan secrets", reopened.get_secret(pid, SecretKind.PASSWORD) == "p@ss")
reopened.delete_profile(pid)
check("delete removes the profile", [p.name for p in reopened.list_profiles()] == ["staging"])
orphans = reopened._conn.execute("SELECT count(*) FROM secrets WHERE profile_id = ?", [pid]).fetchone()
check("delete cascades to secrets", orphans is not None and orphans[0] == 0, str(orphans))
try:
    reopened.get_profile(pid)
    check("missing profile raises", False)
except KeyError:
    check("missing profile raises", True)
reopened.close()

try:
    Vault.create(path, "another")
    check("create refuses to clobber an existing vault", False)
except FileExistsError:
    check("create refuses to clobber an existing vault", True)

print(f"\n{'ALL VAULT CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
