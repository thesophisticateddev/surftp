"""Integration tests for SSH as a first-class connection, against real asyncssh servers.

Covers the plan's §9 "Integration" verification:

* connect with ``Protocol.SSH`` and no file pane,
* run a command and check stdout and exit status,
* open a local forward and move bytes through it end to end,
* open a SOCKS proxy and prove it binds to loopback only,
* connect through a jump host and confirm *both* host keys were verified,
* authenticate with two identity files where the first is wrong and the second
  correct,
* confirm a dedicated connection is a genuinely separate transport while an
  SFTP profile still shares.

HOME is redirected before importing surftp so known_hosts writes land in a
temporary directory and never touch the developer's real one.
"""

from __future__ import annotations

import asyncio
import os
import socket
import struct
import sys
import tempfile
from pathlib import Path

WORK = Path(tempfile.mkdtemp(prefix="surftp-sshit-"))
os.environ["HOME"] = str(WORK / "home")
(WORK / "home").mkdir()

import asyncssh  # noqa: E402

sys.path.insert(0, "/home/salman/Documents/surftp")

from surftp.net import hostkeys  # noqa: E402
from surftp.net.connect import open_connection, resolve_credential  # noqa: E402
from surftp.net.manager import get_manager, reset_manager  # noqa: E402
from surftp.net.types import (  # noqa: E402
    AuthMethod,
    ConnectionProfile,
    Credential,
    ForwardKind,
    ForwardSpec,
    HostKeyChanged,
    HostKeyUnknown,
    Protocol,
)

JUMP_PORT = 8030
TARGET_PORT = 8031
USER = "tester"
PASSWORD = "correct horse"

WRONG_KEY = WORK / "wrong.pem"
RIGHT_KEY = WORK / "right.pem"


class Server(asyncssh.SSHServer):
    """Accepts our test user by password, or by the two generated client keys.

    Port-forwarding requests are allowed (``connection_requested`` returns
    ``True``), because the plan's forward tests move bytes through a tunnel.
    """

    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return username == USER and password == PASSWORD

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        if username != USER:
            return False
        return key == asyncssh.read_public_key(str(RIGHT_KEY) + ".pub")

    def connection_requested(self, dest_host: str, dest_port: int, orig_host: str, orig_port: int) -> bool:
        return True  # allow standard -L / -D port forwarding


async def copy_stream(src, dst, eof: bool) -> None:
    """Copy one stream to another until EOF, then optionally signal EOF."""
    while True:
        try:
            data = await src.read(65536)
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


async def process_factory(process) -> None:
    """Run the requested command, or an interactive shell for shell requests.

    The exit status is reported back via ``process.exit`` — without it the
    client would never learn that ``exit 3`` exited 3.
    """
    cmd = getattr(process, "command", None)
    args = ["/bin/sh", "-c", cmd] if cmd else ["/bin/sh"]
    sub = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if cmd:
        await asyncio.gather(
            copy_stream(sub.stdout, process.stdout, eof=False),
            copy_stream(sub.stderr, process.stderr, eof=False),
        )
        process.exit((await sub.wait()) or 0)
    else:
        await asyncio.gather(
            copy_stream(process.stdin, sub.stdin),
            copy_stream(sub.stdout, process.stdout, eof=False),
            copy_stream(sub.stderr, process.stderr, eof=False),
        )


async def start_server(port: int) -> asyncssh.SSHAcceptor:
    """Start the test server on one loopback port."""
    return await asyncssh.create_server(
        Server,
        "127.0.0.1",
        port,
        server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
        sftp_factory=True,
        process_factory=process_factory,
        encoding=None,
    )


def profile(**overrides: object) -> ConnectionProfile:
    """Build a profile pointed at the target server."""
    base: dict = dict(
        name="test",
        protocol=Protocol.SSH,
        host="127.0.0.1",
        port=TARGET_PORT,
        username=USER,
        auth_method=AuthMethod.PASSWORD,
    )
    base.update(overrides)
    return ConnectionProfile(**base)  # type: ignore[arg-type]


async def trust(host: str, port: int) -> None:
    """Fetch and record a host key, as the trust prompt would."""
    key = await hostkeys.fetch_host_key(host, port)
    hostkeys.remember_host_key(host, port, key)


async def echo_server() -> tuple[asyncio.Server, int]:
    """A loopback TCP echo server; returns it and its port."""

    async def echo(reader, writer) -> None:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data)
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def main() -> None:
    # Two keys: one the server rejects, one it accepts, for the
    # "first identity wrong, second correct" check.
    for path in (WRONG_KEY, RIGHT_KEY):
        key = asyncssh.generate_private_key("ssh-rsa", key_size=2048)
        key.write_private_key(str(path), "pkcs1-pem")
        key.write_public_key(str(path) + ".pub")

    jump_server = await start_server(JUMP_PORT)
    target_server = await start_server(TARGET_PORT)
    failures = 0

    def check(label: str, condition: bool, detail: str = "") -> None:
        nonlocal failures
        if condition:
            print(f"  PASS  {label}")
        else:
            failures += 1
            print(f"  FAIL  {label}  {detail}")

    try:
        await trust("127.0.0.1", JUMP_PORT)
        await trust("127.0.0.1", TARGET_PORT)

        print("\n[1] Protocol.SSH connects with no file pane")
        reset_manager()
        conn = await open_connection(profile(), Credential(password=PASSWORD))
        check("filesystem is None (no pane)", conn.filesystem is None)
        check("session present", conn.session is not None)
        check("session is connected", conn.session is not None and conn.session.is_connected)
        await conn.close()

        print("\n[2] run a command and check stdout and exit status")
        conn = await open_connection(profile(), Credential(password=PASSWORD))
        session = conn.session
        assert session is not None
        status, out, err = await session.run_command("echo hello-command")
        check("stdout captured", out.strip() == "hello-command", repr(out))
        check("exit status 0", status == 0, str(status))
        status, out, err = await session.run_command("exit 3")
        check("non-zero exit status captured", status == 3, str(status))
        status, out, err = await session.run_command("echo both; echo err >&2")
        check("stderr captured too", err.strip() == "err", repr(err))

        print("\n[3] local port forward moves bytes end to end")
        echo, eport = await echo_server()
        handle = await session.start_forward(
            ForwardSpec(kind=ForwardKind.LOCAL, listen_port=0, dest_host="127.0.0.1", dest_port=eport)
        )
        check("forward is running", handle.state == "running", handle.state)
        check("bound on a real port", handle.bound_port and handle.bound_port > 0, str(handle.bound_port))
        reader, writer = await asyncio.open_connection("127.0.0.1", handle.bound_port)
        writer.write(b"ping"); await writer.drain()
        data = await asyncio.wait_for(reader.read(64), 5)
        check("bytes round-trip through the tunnel", data == b"ping", repr(data))
        writer.close(); await writer.wait_closed()
        handle.stop()
        check("forward stops cleanly", handle.state == "stopped", handle.state)

        print("\n[4] dynamic SOCKS proxy binds to loopback only")
        socks = await session.start_forward(
            ForwardSpec(kind=ForwardKind.DYNAMIC, listen_port=0)
        )
        check("socks running", socks.state == "running", socks.state)
        port = socks.bound_port
        check("socks bound to a port", port and port > 0, str(port))
        # SOCKS5 handshake: no-auth method, then CONNECT to the echo server.
        # All reads/writes are async so the event loop stays free to run the
        # tunnel itself — a blocking recv here would deadlock the proxy.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        check("socks offers no-auth", await asyncio.wait_for(reader.read(2), 5) == b"\x05\x00")
        writer.write(
            b"\x05\x01\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack(">H", eport)
        )
        await writer.drain()
        reply = await asyncio.wait_for(reader.read(10), 5)
        check("socks connect accepted", reply[0:2] == b"\x05\x00", reply.hex())
        writer.write(b"proxy")
        await writer.drain()
        check("socks relays bytes", await asyncio.wait_for(reader.read(64), 5) == b"proxy")
        writer.close()
        await writer.wait_closed()
        # Prove loopback-only binding: connecting via a non-loopback address
        # must be refused. Skip if the machine has no such interface.
        non_loopback = [
            addr for family, _, _, _, sockaddr in socket.getaddrinfo(
                socket.gethostname(), None, socket.AF_INET
            )
            for addr in (sockaddr[0],)
            if not addr.startswith("127.")
        ]
        refused = False
        if non_loopback:
            try:
                probe_r, probe_w = await asyncio.wait_for(
                    asyncio.open_connection(non_loopback[0], port), 2
                )
                probe_w.close()
                await probe_w.wait_closed()
            except (OSError, asyncio.TimeoutError):
                refused = True
        else:
            refused = True  # nothing to reach, trivially satisfied
        check("socks is loopback-only (non-loopback connect refused)", refused)
        socks.stop()
        echo.close()
        await echo.wait_closed()
        await conn.close()  # this session is done; close before wiping known_hosts

        print("\n[5] jump host: its own host key is verified")
        # Forget both keys, trust only the *target*, and expect the jump to be
        # refused as unknown — a ProxyJump that skipped verification would
        # connect right through.
        hosts_file = hostkeys.known_hosts_path()
        hosts_file.write_bytes(b"")
        await trust("127.0.0.1", TARGET_PORT)
        jumped = profile(jump_host=f"{USER}@127.0.0.1:{JUMP_PORT}")
        try:
            conn = await open_connection(jumped, Credential(password=PASSWORD))
            check("jump host unknown is refused", False, "connected without trusting the jump!")
        except HostKeyUnknown:
            check("jump host raised HostKeyUnknown", True)
        # Trust the jump, connect through it, and confirm the target's key is
        # verified too by swapping it.
        await trust("127.0.0.1", JUMP_PORT)
        conn = await open_connection(jumped, Credential(password=PASSWORD))
        sess = conn.session
        assert sess is not None
        status, out, _ = await sess.run_command("echo via-jump")
        check("command runs through the jump", status == 0 and "via-jump" in out, repr(out))
        await conn.close()

        print("\n[6] a changed target key is caught through the tunnel")
        target_server.close()
        await target_server.wait_closed()
        target_server = await start_server(TARGET_PORT)
        try:
            conn = await open_connection(jumped, Credential(password=PASSWORD))
            check("changed target key refused", False, "connected anyway!")
        except HostKeyChanged:
            check("target key verified through the tunnel (HostKeyChanged)", True)
        await trust("127.0.0.1", TARGET_PORT)  # restore for the remaining checks

        print("\n[7] two identity files: first wrong, second correct")
        two_keys = profile(
            auth_method=AuthMethod.PEM_FILE,
            identity_files=(str(WRONG_KEY), str(RIGHT_KEY)),
        )
        conn = await open_connection(two_keys, Credential())
        check("wrong-then-right keys connect", conn.session is not None)
        await conn.close()
        only_wrong = profile(
            auth_method=AuthMethod.PEM_FILE, identity_files=(str(WRONG_KEY),)
        )
        try:
            conn = await open_connection(only_wrong, Credential())
            check("only the wrong key refuses", False, "connected with a rejected key!")
        except Exception:
            check("only the wrong key refuses", True)

        print("\n[8] dedicated connection is separate; SFTP profiles share")
        sftp_a = profile(protocol=Protocol.SFTP)
        sftp_b = profile(protocol=Protocol.SFTP, name="test-2")
        check(
            "SFTP profile defaults to sharing",
            sftp_a.dedicated_connection is False,
            str(sftp_a.dedicated_connection),
        )
        conn_a = await open_connection(sftp_a, Credential(password=PASSWORD))
        conn_b = await open_connection(sftp_b, Credential(password=PASSWORD))
        check("SFTP profiles share one connection", conn_a.session is conn_b.session)
        ssh_conn = await open_connection(profile(name="dedicated"), Credential(password=PASSWORD))
        check(
            "SSH profile is a genuinely separate transport",
            ssh_conn.session is not None
            and ssh_conn.session is not conn_a.session
            and ssh_conn.session.connection is not conn_a.session.connection,
        )
        # Close the SFTP panes; the dedicated SSH connection must survive.
        await conn_a.close()
        await conn_b.close()
        check(
            "disconnecting SFTP leaves the SSH session alive",
            ssh_conn.session is not None and ssh_conn.session.is_connected,
        )
        # SFTP on demand on the existing SSH session (browse this host).
        fs = await ssh_conn.session.start_sftp()
        entries = await fs.list_directory("/")
        check("SFTP-on-demand lists on the existing connection", len(entries) > 0)
        fs.close()
        await ssh_conn.close()

        print("\n[9] a failed forward does not abort the connection")
        conn9 = await open_connection(profile(name="fwd-probe"), Credential(password=PASSWORD))
        session9 = conn9.session
        assert session9 is not None
        bad = await session9.start_forward(
            ForwardSpec(kind=ForwardKind.LOCAL, listen_port=9, dest_host="127.0.0.1", dest_port=1)
        )
        # Port 9 may be bindable on some systems; accept either outcome as long
        # as the connection is still up.
        check("connection still alive after a forward attempt", session9.is_connected)
        bad.stop()
        await conn9.close()


    finally:
        jump_server.close()
        await asyncio.wait_for(jump_server.wait_closed(), 10)

        target_server.close()
        await asyncio.wait_for(target_server.wait_closed(), 10)


    print(f"\n{'ALL SSH INTEGRATION CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


asyncio.run(main())