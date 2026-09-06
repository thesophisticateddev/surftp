"""Unit tests for the SSH-as-a-connection feature: profile round-trip, forwards CRUD,
the credential cache, and the connection manager's sharing rules. No network.

Covers the plan's §9 "Unit" verification: the profile round-trips through the
vault with every new field, forwards CRUD works, and the credential cache
clears on lock, disconnect and exit.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, "/home/salman/Documents/surftp")

from surftp.net.credential_cache import CredentialCache, get_credential_cache  # noqa: E402
from surftp.net.connect import RemoteConnection, resolve_credential  # noqa: E402
from surftp.net.manager import SSHConnectionManager  # noqa: E402
from surftp.net.ssh import check_identity_files, connection_key  # noqa: E402
from surftp.net.types import (  # noqa: E402
    AuthMethod,
    ConnectionProfile,
    Credential,
    ForwardKind,
    ForwardSpec,
    Protocol,
    SecretKind,
    key_passphrase_kind,
)
from surftp.store import Vault  # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="surftp-ssh-units-"))
failures = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record one assertion without aborting the rest of the run."""
    global failures
    if condition:
        print(f"  PASS  {label}")
    else:
        failures += 1
        print(f"  FAIL  {label}  {detail}")


def ssh_profile(**overrides) -> ConnectionProfile:
    """A profile exercising every new SSH field."""
    base = dict(
        name="ssh-box",
        protocol=Protocol.SSH,
        host="box.example",
        username="root",
        auth_method=AuthMethod.PEM_FILE,
        pem_path="/keys/first.pem",
        identity_files=("/keys/first.pem", "/keys/second.pem"),
        jump_host="bastion@jump.example:2222",
        remote_command="tmux attach",
        keepalive_interval=60,
        compression=True,
        agent_forwarding=True,
        request_pty=False,
        dedicated_connection=True,
    )
    base.update(overrides)
    return ConnectionProfile(**base)  # type: ignore[arg-type]


print("\n[1] Protocol.SSH is first-class")
check("Protocol.SSH exists", Protocol.SSH == "ssh")
check("default port is 22", ssh_profile().port == 22)
check("SSH profile is_ssh", ssh_profile().is_ssh)
check("SFTP profile is_ssh too", ssh_profile(protocol=Protocol.SFTP).is_ssh)
check(
    "SFTP profile shares (dedicated_connection=False)",
    ssh_profile(protocol=Protocol.SFTP).dedicated_connection is False,
)
check(
    "SSH profile owns its connection (dedicated_connection=True)",
    ssh_profile().dedicated_connection is True,
)
check(
    "identity_files default empty for legacy profiles",
    ssh_profile(protocol=Protocol.SFTP, identity_files=()).identity_files == (),
)
check(
    "key_paths falls back to pem_path",
    ConnectionProfile(
        name="single", protocol=Protocol.SFTP, host="h", username="u",
        auth_method=AuthMethod.PEM_FILE, pem_path="/only.pem",
    ).key_paths
    == ("/only.pem",),
)
check(
    "key_paths prefers identity_files",
    ssh_profile().key_paths == ("/keys/first.pem", "/keys/second.pem"),
)
check(
    "indexed passphrase kind",
    key_passphrase_kind(1) == "key_passphrase:1",
)
loose_key = WORK / "loose.pem"
loose_key.write_text("x")
loose_key.chmod(0o644)
warnings = check_identity_files((str(loose_key), str(WORK / "missing.pem")))
check("group/world-readable key warns", len(warnings) == 1, str(warnings))
loose_key.chmod(0o600)
check("secure key does not warn", check_identity_files((str(loose_key),)) == [])

print("\n[2] profile round-trips through the vault with every new field")
path = WORK / "v.duckdb"
vault = Vault.create(path, "master")
saved = ssh_profile()
pid = vault.save_profile(saved)
round_tripped = vault.get_profile(pid)
check("name kept", round_tripped.name == saved.name)
check("protocol kept", round_tripped.protocol is Protocol.SSH)
check("identity_files kept", round_tripped.identity_files == saved.identity_files)
check("jump_host kept", round_tripped.jump_host == saved.jump_host)
check("remote_command kept", round_tripped.remote_command == saved.remote_command)
check("keepalive_interval kept", round_tripped.keepalive_interval == 60)
check("compression kept", round_tripped.compression is True)
check("agent_forwarding kept", round_tripped.agent_forwarding is True)
check("request_pty kept", round_tripped.request_pty is False)
check("dedicated_connection kept", round_tripped.dedicated_connection is True)
check("pem_path kept", round_tripped.pem_path == saved.pem_path)

print("\n[3] forwards CRUD")
f1 = vault.save_forward(
    pid, ForwardSpec(kind=ForwardKind.LOCAL, listen_port=8080, dest_host="db", dest_port=5432)
)
f2 = vault.save_forward(pid, ForwardSpec(kind=ForwardKind.DYNAMIC, listen_port=1080, auto_start=False))
f3 = vault.save_forward(
    pid,
    ForwardSpec(kind=ForwardKind.SOCKET, listen_path="/tmp/docker.sock", dest_path="/var/run/docker.sock"),
)
listed = vault.list_forwards(pid)
check("three forwards saved", len(listed) == 3, str(len(listed)))
check("local forward kept", listed[0].dest_host == "db" and listed[0].dest_port == 5432)
check("dynamic forward kept", listed[1].kind is ForwardKind.DYNAMIC and not listed[1].auto_start)
check("socket forward kept", listed[2].listen_path == "/tmp/docker.sock")
fetched = vault.get_forward(f2)
check("get_forward round-trips", fetched.dest_port is None and fetched.auto_start is False)
check("default listen host is loopback", listed[0].listen_host == "127.0.0.1")
check(
    "description renders like ssh -L",
    listed[0].description == "127.0.0.1:8080 -> db:5432",
    listed[0].description,
)
check(
    "socket description",
    listed[2].description == "/tmp/docker.sock -> /var/run/docker.sock",
)
vault.delete_forward(f3)
check("delete removes one row", len(vault.list_forwards(pid)) == 2)
other = vault.save_profile(ssh_profile(name="other", host="elsewhere.example"))
check("forwards are per-profile", vault.list_forwards(other) == [])

print("\n[4] indexed secret kinds (per-key passphrases)")
vault.put_secret(pid, key_passphrase_kind(0), "pp-zero")
vault.put_secret(pid, key_passphrase_kind(1), "pp-one")
check("index 0 round-trips", vault.get_secret(pid, key_passphrase_kind(0)) == "pp-zero")
check("index 1 round-trips", vault.get_secret(pid, key_passphrase_kind(1)) == "pp-one")
check("missing index is None", vault.get_secret(pid, key_passphrase_kind(2)) is None)
check("legacy single row untouched", vault.get_secret(pid, SecretKind.KEY_PASSPHRASE) is None)
vault.delete_profile(pid)
check("delete cascades forwards", vault.list_forwards(pid) == [])

print("\n[5] the in-memory credential cache")
cache = CredentialCache()
prof = ssh_profile(id=7)
other_prof = ssh_profile(id=8, host="other.example")
check("empty cache misses", cache.get(prof) is None)
cache.put(prof, Credential(password="s3cret"))
check("cached credential returns", cache.get(prof) is not None)
check("different profile misses", cache.get(other_prof) is None)
check("disconnect drops one profile", (cache.drop(prof), cache.get(prof))[1] is None)
cache.put(prof, Credential(password="again"))
cache.clear()
check("clear (vault lock / exit) empties the cache", len(cache) == 0)
anon = ssh_profile(id=None, name="anon")
temp2 = ssh_profile(id=None, name="anon2")
cache.put(anon, Credential(password="temp"))
check("unsaved profiles share a temp key", cache.get(temp2) is not None)
check("empty credentials are not cached", (cache.put(prof, Credential()), len(cache))[1] == 1)

print("\n[6] resolve_credential consults the cache before prompting (no second prompt)")
pw_prof = ssh_profile(id=9, protocol=Protocol.SFTP, auth_method=AuthMethod.PASSWORD, jump_host=None)
default_cache = get_credential_cache()
default_cache.clear()
default_cache.put(pw_prof, Credential(password="from-cache"))
prompts: list[str] = []

async def ask(message: str) -> str:
    prompts.append(message)
    return "should-not-prompt"

cred = asyncio.run(resolve_credential(pw_prof, None, None, ask))
check("cached password used, no prompt", cred.password == "from-cache" and not prompts, str(prompts))
default_cache.clear()

print("\n[7] resolve_credential resolves multiple identity-file passphrases")
multi = ssh_profile(identity_files=("/k0.pem", "/k1.pem"))
default_cache.clear()
default_cache.put(multi, Credential(key_passphrases=("first", None)))
cred = asyncio.run(resolve_credential(multi, None, None, ask))
check("key 0 passphrase resolved", cred.passphrase_for(0) == "first")
check("key 1 passphrase unresolved", cred.passphrase_for(1) is None)
default_cache.clear()

print("\n[8] RemoteConnection.filesystem is None for an SSH connection (no pane)")
conn = RemoteConnection(profile=ssh_profile(), filesystem=None, initial_path="", session=object())
check("is_ssh_session true", conn.is_ssh_session)
check("filesystem None", conn.filesystem is None)

print("\n[9] connection key names the address, not the profile")
a = ssh_profile(name="one")
b = ssh_profile(name="two")
c = ssh_profile(name="three", username="someoneelse")
check("same address shares a key", connection_key(a) == connection_key(b))
check("different user differs", connection_key(a) != connection_key(c))

print("\n[10] manager sharing: dedicated forces a new connection, sharing reuses one")

class StubConn:
    """The bit of an asyncssh connection the manager touches."""

    def is_closed(self) -> bool:
        return False

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass

async def sharing() -> None:
    manager = SSHConnectionManager()
    from surftp.net.ssh import SSHSession

    async def fake_connect(profile, credential, *, tunnel=None):
        session = SSHSession.__new__(SSHSession)
        session._profile = profile
        session._forwards = {}
        session._connected_at = 0.0
        session._connection = StubConn()
        return session

    SSHSession.connect = fake_connect  # type: ignore[assignment]
    try:
        plain = ssh_profile(dedicated_connection=False, jump_host=None)
        first = await manager.acquire(plain, Credential(password="x"))
        second = await manager.acquire(plain, Credential(password="x"))
        check("SFTP profile shares the connection", first is second)
        dedicated = await manager.acquire(
            ssh_profile(dedicated_connection=True, jump_host=None, host="other.example"),
            Credential(password="x"),
        )
        check("SSH profile gets its own connection", dedicated is not first)
        check("two sessions live", len(manager.sessions()) == 2)
        await manager.release(second)
        check("one release keeps the shared connection alive", manager.is_owned(first))
        await manager.release(first)
        check("last release closes the shared connection", not manager.is_owned(first))
    finally:
        del SSHSession.connect

asyncio.run(sharing())

vault.close()
print(f"\n{'ALL SSH UNIT CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)