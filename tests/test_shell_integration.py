"""Integration test for the interactive shell over a real local SSH server.

Uses asyncssh's own server side with a ``process_factory`` that bridges the
SSH streams to a real ``/bin/sh`` subprocess — the closest thing to a real
interactive remote shell available without a container.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

WORK = Path(tempfile.mkdtemp(prefix="surftp-shell-"))
os.environ["HOME"] = str(WORK / "home")
(WORK / "home").mkdir()

import asyncssh  # noqa: E402

sys.path.insert(0, "/home/salman/Documents/surftp")

from surftp.net.connect import open_connection  # noqa: E402
from surftp.net.types import AuthMethod, ConnectionProfile, Credential, Protocol  # noqa: E402
from surftp.net import hostkeys  # noqa: E402
from surftp.shell.session import ShellSession  # noqa: E402

PORT = 8023
USER = "tester"
PASSWORD = "correct horse"


class Server(asyncssh.SSHServer):
    """Accepts our test user by password and runs real commands."""

    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return username == USER and password == PASSWORD


async def _bridge(src, dst, *, eof: bool = True) -> None:
    """Copy one stream to another until EOF, then optionally signal EOF.

    A ``TerminalSizeChanged`` exception arrives whenever the client calls
    ``change_terminal_size``; the subprocess has no PTY to resize, so it is
    swallowed and the bridge keeps copying.
    """
    while True:
        try:
            data = await src.read(65536)
        except asyncssh.TerminalSizeChanged:
            continue
        except (OSError, asyncssh.Error):
            break
        if not data:
            break
        dst.write(data)
        await dst.drain()
    if eof:
        try:
            dst.write_eof()
        except Exception:
            pass


async def shell_factory(process) -> None:
    """Bridge the SSH process streams to a real /bin/sh subprocess."""
    sub = await asyncio.create_subprocess_exec(
        "/bin/sh",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.gather(
        _bridge(process.stdin, sub.stdin),
        _bridge(sub.stdout, process.stdout, eof=False),
        _bridge(sub.stderr, process.stderr, eof=False),
    )


def profile(**overrides) -> ConnectionProfile:
    base: dict = dict(
        name="shell-test",
        protocol=Protocol.SFTP,
        host="127.0.0.1",
        port=PORT,
        username=USER,
        auth_method=AuthMethod.PASSWORD,
    )
    base.update(overrides)
    return ConnectionProfile(**base)  # type: ignore[arg-type]


async def main() -> int:
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.create_server(
        Server,
        "127.0.0.1",
        PORT,
        server_host_keys=[host_key],
        sftp_factory=True,
        process_factory=shell_factory,
        encoding=None,
    )
    failures = 0

    def check(label: str, condition: bool, detail: str = "") -> None:
        nonlocal failures
        if condition:
            print(f"  PASS  {label}")
        else:
            failures += 1
            print(f"  FAIL  {label}  {detail}")

    try:
        fetched = await hostkeys.fetch_host_key("127.0.0.1", PORT)
        hostkeys.remember_host_key("127.0.0.1", PORT, fetched)

        conn = await open_connection(profile(), Credential(password=PASSWORD))
        session = conn.session
        check("session is ssh", session is not None, "no session")

        print("\n[1] run echo over the shell and read the frame")
        shell = await ShellSession.start(session, 80, 24)
        await shell.send_input(b"echo hello-shell\n")
        frames: list[dict] = []
        deadline = asyncio.get_event_loop().time() + 5
        got = False
        while asyncio.get_event_loop().time() < deadline and not got:
            # The pipe read blocks; it must run on a worker thread so the
            # event loop stays free to drain the SSH channel (the plan's
            # isolation claim in action).
            message = await asyncio.to_thread(shell.next_message, 0.1)
            if message is not None:
                frames.append(message)
                all_text = "".join(run[0] for row in message["rows"] for run in row)
                if "hello-shell" in all_text:
                    got = True
        check("shell echoed output", got, f"{len(frames)} frames")
        await shell.close()

        print("\n[2] SFTP still works while a shell is open (concurrent channels)")
        shell2 = await ShellSession.start(session, 80, 24)
        entries = await conn.filesystem.list_directory("/")
        check("sftp lists while shell open", len(entries) > 0, str(len(entries)))
        await shell2.close()
        await conn.close()

        print("\n[3] child process is cleaned up on close")
        # open a fresh connection for this one since the previous was closed
        conn2 = await open_connection(profile(), Credential(password=PASSWORD))
        shell3 = await ShellSession.start(conn2.session, 80, 24)
        pid = shell3._process.pid
        await shell3.close()
        await asyncio.sleep(0.3)
        check("child terminated", not shell3._process.is_alive(), f"pid {pid} still alive")
        await conn2.close()
    finally:
        server.close()
        await server.wait_closed()

    print(f"\n{'ALL SHELL INTEGRATION CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))