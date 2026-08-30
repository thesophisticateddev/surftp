"""Transfer engine tests against a real SFTP server.

These exist because uploads shipped stuck at 0% and downloads shipped silently
producing empty files: `SFTPFileSystem` opened remote files in asyncssh's TEXT
mode, so writing bytes raised AttributeError (swallowed by the per-item task,
leaving RUNNING forever) and reading returned str (coerced to b"", i.e. EOF).
Both directions are now checked byte-for-byte.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import tempfile
from pathlib import Path

WORK = Path(tempfile.mkdtemp(prefix="surftp-xfer-"))
os.environ["HOME"] = str(WORK / "home")
(WORK / "home").mkdir()

sys.path.insert(0, "/home/salman/Documents/surftp")

import asyncssh  # noqa: E402

from surftp.fs import LocalFileSystem  # noqa: E402
from surftp.net import hostkeys  # noqa: E402
from surftp.net.connect import open_connection  # noqa: E402
from surftp.net.types import AuthMethod, ConnectionProfile, Credential, Protocol  # noqa: E402
from surftp.transfer.engine import TransferEngine  # noqa: E402
from surftp.transfer.planner import plan_transfer  # noqa: E402
from surftp.transfer.types import ConflictPolicy, TransferJob, TransferState  # noqa: E402

PORT = 8044
USER = "tester"
PASSWORD = "pw"
REMOTE = WORK / "remote"
LOCAL = WORK / "local"
DOWNLOADS = WORK / "downloads"
for d in (REMOTE, LOCAL, DOWNLOADS):
    d.mkdir()

failures = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record one assertion without aborting the run."""
    global failures
    if condition:
        print(f"  PASS  {label}")
    else:
        failures += 1
        print(f"  FAIL  {label}  {detail}")


def digest(path: Path) -> str:
    """BLAKE2b of a file, for byte-for-byte comparison."""
    h = hashlib.blake2b()
    h.update(path.read_bytes())
    return h.hexdigest()


class Server(asyncssh.SSHServer):
    """Password-only test server."""

    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return username == USER and password == PASSWORD


async def run_job(source, destination, src_path: str, dest_dir: str, policy=ConflictPolicy.OVERWRITE):
    """Plan and run a single-file transfer, returning (result, seen_progress)."""
    entries = [e for e in await source.list_directory(os.path.dirname(src_path))
               if e.path == src_path]
    items = await plan_transfer(source, src_path, dest_dir, entries=entries)
    job = TransferJob(items=items, total_bytes=sum(i.size for i in items),
                      source_label="src", destination_label="dst")
    seen: list[tuple[str, int]] = []
    engine = TransferEngine(source, destination,
                            on_progress=lambda p: seen.append((p.state, p.bytes_done)),
                            conflict_policy=policy)
    result = await asyncio.wait_for(engine.run(job), timeout=60)
    return result, seen


async def main() -> None:
    # Binary content with bytes that are not valid UTF-8: text mode would
    # corrupt or reject these, which is the whole point.
    payload = os.urandom(2_000_000)
    (LOCAL / "payload.bin").write_bytes(payload)
    (REMOTE / "remote-payload.bin").write_bytes(payload)
    (LOCAL / "tiny.txt").write_text("hello\n")

    host_key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.create_server(
        Server, "127.0.0.1", PORT, server_host_keys=[host_key], sftp_factory=True
    )
    hostkeys.remember_host_key("127.0.0.1", PORT,
                               await hostkeys.fetch_host_key("127.0.0.1", PORT))
    profile = ConnectionProfile(name="t", protocol=Protocol.SFTP, host="127.0.0.1", port=PORT,
                                username=USER, auth_method=AuthMethod.PASSWORD,
                                remote_path=str(REMOTE))
    conn = await open_connection(profile, Credential(password=PASSWORD))
    local = LocalFileSystem()
    remote = conn.filesystem

    try:
        print("\n[1] upload: local -> SFTP")
        result, seen = await run_job(local, remote, str(LOCAL / "payload.bin"), str(REMOTE))
        dest = REMOTE / "payload.bin"
        check("one file completed", result.completed == 1, str(result))
        check("nothing failed", result.failed == 0, str(result))
        check("destination exists", dest.exists())
        check("size matches", dest.exists() and dest.stat().st_size == len(payload),
              str(dest.stat().st_size if dest.exists() else None))
        check("bytes match exactly", dest.exists() and digest(dest) == digest(LOCAL / "payload.bin"))
        check("progress actually advanced", max((b for _, b in seen), default=0) == len(payload),
              str(max((b for _, b in seen), default=0)))
        check("no partial file left behind",
              not any(p.name.startswith(".surftp-partial-") for p in REMOTE.iterdir()),
              str([p.name for p in REMOTE.iterdir()]))

        print("\n[2] download: SFTP -> local")
        result, seen = await run_job(remote, local, str(REMOTE / "remote-payload.bin"), str(DOWNLOADS))
        got = DOWNLOADS / "remote-payload.bin"
        check("one file completed", result.completed == 1, str(result))
        check("downloaded file is NOT empty", got.exists() and got.stat().st_size > 0,
              str(got.stat().st_size if got.exists() else None))
        check("size matches", got.exists() and got.stat().st_size == len(payload))
        check("bytes match exactly", got.exists() and digest(got) == digest(REMOTE / "remote-payload.bin"))

        print("\n[3] a small text file survives the round trip")
        await run_job(local, remote, str(LOCAL / "tiny.txt"), str(REMOTE))
        check("tiny file matches", (REMOTE / "tiny.txt").read_text() == "hello\n",
              repr((REMOTE / "tiny.txt").read_text() if (REMOTE / "tiny.txt").exists() else None))

        print("\n[4] a failing transfer reports FAILED, it does not hang at 0%")
        readonly = REMOTE / "readonly"
        readonly.mkdir()
        readonly.chmod(0o500)
        try:
            result, seen = await run_job(local, remote, str(LOCAL / "payload.bin"), str(readonly))
            check("marked failed", result.failed == 1, str(result))
            check("did not report success", result.completed == 0, str(result))
            states = {state for state, _ in seen}
            check("never left stuck in RUNNING",
                  TransferState.FAILED in states and TransferState.RUNNING in states, str(states))
        finally:
            readonly.chmod(0o700)
    finally:
        await conn.close()
        server.close()
        await server.wait_closed()

    print(f"\n{'ALL TRANSFER CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


asyncio.run(main())
