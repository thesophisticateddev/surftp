"""End-to-end connection tests against a real, locally hosted SSH/SFTP server.

Uses asyncssh's own server side rather than a container: it is a genuine SSH
protocol exchange (key exchange, host key, auth, SFTP subsystem), which is what
these tests need to prove, without requiring Docker.

HOME is redirected before importing surftp so known_hosts writes land in a
temporary directory and never touch the developer's real one.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

WORK = Path(tempfile.mkdtemp(prefix="surftp-it-"))
os.environ["HOME"] = str(WORK / "home")
(WORK / "home").mkdir()

import asyncssh  # noqa: E402

sys.path.insert(0, "/home/salman/Documents/surftp")

from surftp.net.connect import open_connection, resolve_credential  # noqa: E402
from surftp.net.types import (  # noqa: E402
    AuthMethod,
    ConnectionProfile,
    Credential,
    HostKeyChanged,
    HostKeyUnknown,
    NetworkError,
    Protocol,
)
from surftp.net import hostkeys  # noqa: E402
from surftp.store import Vault  # noqa: E402
from surftp.net.types import SecretKind  # noqa: E402

PORT = 8022
USER = "tester"
PASSWORD = "correct horse"

SERVE_ROOT = WORK / "served"
SERVE_ROOT.mkdir()
(SERVE_ROOT / "alpha.txt").write_text("a" * 1234)
(SERVE_ROOT / "subdir").mkdir()
(SERVE_ROOT / "subdir" / "nested.bin").write_bytes(b"\0" * 10)
(SERVE_ROOT / ".hidden").write_text("x")

CLIENT_KEY = WORK / "client.pem"
ENCRYPTED_KEY = WORK / "client-encrypted.pem"
OPENSSH_ENCRYPTED_KEY = WORK / "client-openssh-encrypted.key"
KEY_PASSPHRASE = "keypass"


class Server(asyncssh.SSHServer):
    """Accepts our test user by password or by the generated client key."""

    def begin_auth(self, username: str) -> bool:
        return True  # authentication is required

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return username == USER and password == PASSWORD

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        return username == USER and key == asyncssh.read_public_key(str(CLIENT_KEY) + ".pub")


def profile(**overrides: object) -> ConnectionProfile:
    """Build a profile pointed at the test server."""
    base: dict = dict(
        name="test",
        protocol=Protocol.SFTP,
        host="127.0.0.1",
        port=PORT,
        username=USER,
        auth_method=AuthMethod.PASSWORD,
        remote_path=str(SERVE_ROOT),
    )
    base.update(overrides)
    return ConnectionProfile(**base)  # type: ignore[arg-type]


async def start_server(host_key: asyncssh.SSHKey) -> asyncssh.SSHAcceptor:
    """Start the SFTP server on the loopback interface."""
    return await asyncssh.create_server(
        Server, "127.0.0.1", PORT, server_host_keys=[host_key], sftp_factory=True
    )


async def main() -> None:
    # An RSA key exported as PKCS#1 PEM: exactly the shape of an AWS-issued
    # .pem, which is the format users are most likely to arrive with.
    key = asyncssh.generate_private_key("ssh-rsa", key_size=2048)
    key.write_private_key(str(CLIENT_KEY), "pkcs1-pem")
    key.write_public_key(str(CLIENT_KEY) + ".pub")
    # Both encrypted formats: PKCS#8 PEM, and the OpenSSH format ssh-keygen
    # produces by default (which needs bcrypt to decrypt).
    key.write_private_key(str(ENCRYPTED_KEY), "pkcs8-pem", passphrase=KEY_PASSPHRASE)
    key.write_private_key(str(OPENSSH_ENCRYPTED_KEY), "openssh", passphrase=KEY_PASSPHRASE)

    host_key = asyncssh.generate_private_key("ssh-ed25519")
    server = await start_server(host_key)
    failures = 0

    def check(label: str, condition: bool, detail: str = "") -> None:
        nonlocal failures
        if condition:
            print(f"  PASS  {label}")
        else:
            failures += 1
            print(f"  FAIL  {label}  {detail}")

    try:
        print("\n[1] unknown host key is refused and reported with a fingerprint")
        try:
            await open_connection(profile(), Credential(password=PASSWORD))
            check("unknown host raises", False, "connected without trust!")
        except HostKeyUnknown as exc:
            check("unknown host raises HostKeyUnknown", True)
            check("fingerprint shown", exc.fingerprint.startswith("SHA256:"), exc.fingerprint)
            check("key type shown", exc.key_type == "ssh-ed25519", exc.key_type)
            fetched = await hostkeys.fetch_host_key("127.0.0.1", PORT)
            hostkeys.remember_host_key("127.0.0.1", PORT, fetched)

        print("\n[2] password auth over SFTP, after trusting the host")
        conn = await open_connection(profile(), Credential(password=PASSWORD))
        entries = await conn.filesystem.list_directory(str(SERVE_ROOT))
        names = [e.name for e in entries]
        check("parent link first", names[0] == "..", str(names))
        check("dirs before files", names[1] == "subdir", str(names))
        check("file listed with size", any(e.name == "alpha.txt" and e.size == 1234 for e in entries))
        check("dotfile present in raw listing", ".hidden" in names, str(names))
        check("mtime populated", all(e.modified > 0 for e in entries if e.name != ".."))
        check("label is user@host", conn.filesystem.label == f"{USER}@127.0.0.1:{PORT}")
        sub = await conn.filesystem.list_directory(str(SERVE_ROOT / "subdir"))
        check("nested listing", [e.name for e in sub][1:] == ["nested.bin"], str(sub))
        check("parent_of is posix", await conn.filesystem.parent_of("/a/b/c") == "/a/b")
        check("is_directory true", await conn.filesystem.is_directory(str(SERVE_ROOT)))
        check("is_directory false", not await conn.filesystem.is_directory(str(SERVE_ROOT / "alpha.txt")))
        await conn.close()

        print("\n[3] wrong password is reported as a rejected password")
        try:
            await open_connection(profile(), Credential(password="nope"))
            check("wrong password rejected", False, "connected anyway!")
        except NetworkError as exc:
            check("names the password", "rejected the password" in str(exc), str(exc))

        print("\n[4] .pem key auth, unencrypted and passphrase-protected")
        conn = await open_connection(
            profile(auth_method=AuthMethod.PEM_FILE, pem_path=str(CLIENT_KEY)), Credential()
        )
        check("unencrypted .pem connects", conn.filesystem.label.endswith(str(PORT)))
        await conn.close()

        conn = await open_connection(
            profile(auth_method=AuthMethod.PEM_FILE, pem_path=str(ENCRYPTED_KEY)),
            Credential(key_passphrase=KEY_PASSPHRASE),
        )
        check("encrypted .pem connects with passphrase", True)
        await conn.close()

        try:
            await open_connection(
                profile(auth_method=AuthMethod.PEM_FILE, pem_path=str(ENCRYPTED_KEY)),
                Credential(key_passphrase="wrong"),
            )
            check("wrong passphrase rejected", False, "connected anyway!")
        except NetworkError as exc:
            check("wrong passphrase named distinctly", "Wrong passphrase" in str(exc), str(exc))

        conn = await open_connection(
            profile(auth_method=AuthMethod.PEM_FILE, pem_path=str(OPENSSH_ENCRYPTED_KEY)),
            Credential(key_passphrase=KEY_PASSPHRASE),
        )
        check("encrypted openssh-format key connects (needs bcrypt)", True)
        await conn.close()

        try:
            await open_connection(
                profile(auth_method=AuthMethod.PEM_FILE, pem_path=str(ENCRYPTED_KEY)), Credential()
            )
            check("missing passphrase rejected", False, "connected anyway!")
        except NetworkError as exc:
            check("missing passphrase named distinctly", "passphrase-protected" in str(exc), str(exc))

        malformed = WORK / "bad.pem"
        malformed.write_text("-----BEGIN RSA PRIVATE KEY-----\nnot a key\n-----END RSA PRIVATE KEY-----\n")
        try:
            await open_connection(
                profile(auth_method=AuthMethod.PEM_FILE, pem_path=str(malformed)), Credential()
            )
            check("malformed key rejected", False, "connected anyway!")
        except NetworkError as exc:
            check("malformed key names the file", "Could not read" in str(exc), str(exc))

        print("\n[5] vault-stored key (PEM_STORED) and vault-stored password")
        vault = Vault.create(WORK / "vault.duckdb", "master-pw")
        stored = profile(auth_method=AuthMethod.PEM_STORED, pem_path=str(CLIENT_KEY))
        pid = vault.save_profile(stored)
        vault.put_secret(pid, SecretKind.PRIVATE_KEY, CLIENT_KEY.read_text())
        saved = stored.with_id(pid)
        cred = await resolve_credential(saved, vault)
        conn = await open_connection(saved, cred)
        check("connects with key material from the vault", True)
        await conn.close()

        pw_profile = profile(name="pw").with_id(vault.save_profile(profile(name="pw")))
        assert pw_profile.id is not None
        vault.put_secret(pw_profile.id, SecretKind.PASSWORD, PASSWORD)
        conn = await open_connection(pw_profile, await resolve_credential(pw_profile, vault))
        check("connects with password from the vault", True)
        await conn.close()

        vault.lock()
        prompted: list[str] = []

        async def ask(message: str) -> str:
            prompted.append(message)
            return PASSWORD

        cred = await resolve_credential(pw_profile, vault, None, ask)
        check("locked vault falls through to a prompt", len(prompted) == 1, str(prompted))
        conn = await open_connection(pw_profile, cred)
        check("prompted credential connects", True)
        await conn.close()
        vault.close()

        print("\n[6] SCP profile browses over SFTP on the same connection")
        conn = await open_connection(profile(protocol=Protocol.SCP), Credential(password=PASSWORD))
        check("scp profile lists via sftp", len(await conn.filesystem.list_directory(str(SERVE_ROOT))) > 1)
        check("session is reusable for channels", conn.session is not None and conn.session.is_connected)
        await conn.close()

        print("\n[7] a changed host key is a hard failure")
        server.close()
        await server.wait_closed()
        server = await start_server(asyncssh.generate_private_key("ssh-ed25519"))
        try:
            await open_connection(profile(), Credential(password=PASSWORD))
            check("changed key refused", False, "connected anyway!")
        except HostKeyChanged as exc:
            check("changed key raises HostKeyChanged", True)
            check("message says verify out of band", "out of band" in str(exc), str(exc))
        except HostKeyUnknown:
            check("changed key not misreported as unknown", False)

        print("\n[8] unreachable host and bad DNS are distinct messages")
        try:
            await open_connection(profile(port=9), Credential(password=PASSWORD))
            check("closed port refused", False)
        except NetworkError as exc:
            check("closed port message", "refused" in str(exc) or "reach" in str(exc), str(exc))
        try:
            await open_connection(profile(host="nosuchhost.invalid"), Credential(password=PASSWORD))
            check("bad dns refused", False)
        except NetworkError as exc:
            check("bad dns message", "resolve" in str(exc), str(exc))

    finally:
        server.close()
        await server.wait_closed()

    print(f"\n{'ALL INTEGRATION CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


asyncio.run(main())
