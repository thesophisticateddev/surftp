"""FTP backend tests against a real aioftp server on loopback."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/home/salman/Documents/surftp")

import aioftp  # noqa: E402

from surftp.fs.types import FileSystemError  # noqa: E402
from surftp.net.ftp import FTPFileSystem  # noqa: E402
from surftp.net.types import AuthMethod, ConnectionProfile, Credential, NetworkError, Protocol  # noqa: E402

PORT = 8021
USER = "ftpuser"
PASSWORD = "ftppass"
ROOT = Path(tempfile.mkdtemp(prefix="surftp-ftp-"))


def profile(**overrides: object) -> ConnectionProfile:
    """Build a profile pointed at the test FTP server."""
    base: dict = dict(
        name="ftp",
        protocol=Protocol.FTP,
        host="127.0.0.1",
        port=PORT,
        username=USER,
        auth_method=AuthMethod.PASSWORD,
    )
    base.update(overrides)
    return ConnectionProfile(**base)  # type: ignore[arg-type]


async def main() -> None:
    (ROOT / "report.csv").write_text("x" * 4096)
    (ROOT / "uploads").mkdir()
    (ROOT / "uploads" / "inner.txt").write_text("hi")

    user = aioftp.User(USER, PASSWORD, home_path="/", base_path=ROOT)
    server = aioftp.Server([user])
    await server.start("127.0.0.1", PORT)
    failures = 0

    def check(label: str, condition: bool, detail: str = "") -> None:
        nonlocal failures
        if condition:
            print(f"  PASS  {label}")
        else:
            failures += 1
            print(f"  FAIL  {label}  {detail}")

    try:
        print("\n[1] login and listing")
        fs = await FTPFileSystem.connect(profile(), Credential(password=PASSWORD))
        entries = await fs.list_directory("/")
        names = [e.name for e in entries]
        check("dirs before files", names == ["uploads", "report.csv"], str(names))
        check("size parsed", any(e.name == "report.csv" and e.size == 4096 for e in entries))
        check("mtime parsed", all(e.modified > 0 for e in entries))
        check("no parent link at root", ".." not in names, str(names))
        check("label", fs.label == f"{USER}@127.0.0.1:{PORT}")

        sub = await fs.list_directory("/uploads")
        subnames = [e.name for e in sub]
        check("parent link below root", subnames[0] == "..", str(subnames))
        check("nested file listed", "inner.txt" in subnames, str(subnames))
        check("is_directory", await fs.is_directory("/uploads"))
        check("working directory", await fs.working_directory() == "/")

        print("\n[2] failures are specific")
        try:
            await fs.list_directory("/nope")
            check("missing dir errors", False)
        except FileSystemError as exc:
            check("missing dir -> FileSystemError", True, str(exc))
        await fs.close()

        try:
            await FTPFileSystem.connect(profile(), Credential(password="wrong"))
            check("bad password rejected", False, "logged in anyway!")
        except NetworkError as exc:
            check("bad password names the login", "rejected the FTP login" in str(exc), str(exc))

        try:
            await FTPFileSystem.connect(profile(port=9), Credential(password=PASSWORD))
            check("closed port refused", False)
        except NetworkError as exc:
            check("closed port message", "Cannot connect" in str(exc), str(exc))
    finally:
        await server.close()

    print(f"\n{'ALL FTP CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


asyncio.run(main())
