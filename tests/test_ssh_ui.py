"""UI pilot test for SSH as a first-class connection.

Covers the plan's §9 "UI (pilot)" verification:

* an SSH profile opens a terminal tab and no file pane,
* the connection panel lists the session and its forwards,
* disconnecting SFTP leaves the SSH session alive — the independence this plan
  exists for.

Drives a real app against a real asyncssh server, exactly like
``test_sessions.py`` does for the pane flow.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

WORK = Path(tempfile.mkdtemp(prefix="surftp-sshui-"))
os.environ["HOME"] = str(WORK / "home")
(WORK / "home").mkdir()

sys.path.insert(0, "/home/salman/Documents/surftp")

import asyncssh  # noqa: E402
from textual.widgets import Button, Input, Select  # noqa: E402

from surftp.app import SurfFTPApp  # noqa: E402
from surftp.net.manager import get_manager  # noqa: E402
from surftp.widgets.connect import ConnectDialog  # noqa: E402
from surftp.widgets.dialogs import HostKeyScreen  # noqa: E402
from surftp.widgets.ssh_screens import RunCommandScreen  # noqa: E402

PORT = 8037
USER = "tester"
PASSWORD = "pw"
SERVE = WORK / "served"
SERVE.mkdir()
(SERVE / "remote-only.txt").write_text("hello")

failures = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record one assertion without aborting the run."""
    global failures
    if condition:
        print(f"  PASS  {label}")
    else:
        failures += 1
        print(f"  FAIL  {label}  {detail}")


class Server(asyncssh.SSHServer):
    """Password-only server with a shell/command process factory."""

    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return username == USER and password == PASSWORD


async def copy_stream(src, dst, eof: bool = True) -> None:
    """Copy one stream to another until EOF, then optionally signal EOF.

    ``TerminalSizeChanged`` is a plain exception (not an ``asyncssh.Error``),
    raised when the client resizes the shell; swallowing it keeps the bridge
    alive — a leaked resize would otherwise tear down the whole connection.
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


async def process_factory(process) -> None:
    """Run the requested command, or an interactive shell."""
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


async def connect_via_dialog(app: SurfFTPApp, pilot, protocol: str) -> None:
    """Fill the connect dialog for the test server and submit it."""
    await pilot.press("f9")
    await pilot.pause(0.4)
    screen = app.screen
    assert isinstance(screen, ConnectDialog), type(screen)
    screen.query_one("#protocol", Select).value = protocol
    screen.query_one("#host", Input).value = "127.0.0.1"
    screen.query_one("#port", Input).value = str(PORT)
    screen.query_one("#username", Input).value = USER
    screen.query_one("#password", Input).value = PASSWORD
    if protocol == "sftp":
        screen.query_one("#remote_path", Input).value = str(SERVE)
    screen.query_one("#connect", Button).press()
    await pilot.pause(0.6)
    if isinstance(app.screen, HostKeyScreen):
        app.screen.query_one("#trust", Button).press()
    await pilot.pause(1.2)


async def main() -> None:
    # Under App.run_test(), Textual redirects sys.stdout/stderr to objects whose
    # fileno() is -1. The shell's emulator is spawned with the multiprocessing
    # "spawn" context, and CPython's resource tracker passes sys.stderr.fileno()
    # when it first launches — a -1 there makes the spawn fail. Pre-starting the
    # tracker here (where stderr is still a real fd) keeps it alive for the
    # whole process, so the shells the app opens during the test spawn fine.
    import multiprocessing.resource_tracker  # noqa: E402

    multiprocessing.resource_tracker.ensure_running()

    host_key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.create_server(
        Server,
        "127.0.0.1",
        PORT,
        server_host_keys=[host_key],
        sftp_factory=True,
        process_factory=process_factory,
        encoding=None,
    )
    app = SurfFTPApp(vault_path=WORK / "vault.duckdb")
    try:
        async with app.run_test() as pilot:
            await pilot.pause(0.4)

            print("\n[1] an SSH profile opens a terminal tab and no file pane")
            await connect_via_dialog(app, pilot, "ssh")
            check("left pane is still local (no file pane)", not app.left_pane.is_remote)
            check("no session tab was opened", app.left_sessions.session_count == 0,
                  str(app.left_sessions.session_count))
            check("a terminal tab was opened", app.bottom_panel.has_shells)
            check("the session is tracked as SSH-only", len(app._ssh_sessions) == 1,
                  str(app._ssh_sessions))
            ssh_conn = next(iter(app._ssh_sessions.values()))
            check("session is connected", ssh_conn.session is not None and ssh_conn.session.is_connected)
            check("manager owns the connection", len(get_manager().sessions()) == 1)

            print("\n[2] the connection panel lists the session")
            await pilot.press("f11")
            await pilot.pause(0.5)
            panel = app.bottom_panel.connections
            rows = panel.table.row_count
            check("panel lists one connection", rows == 1, str(rows))
            first_row = [str(panel.table.get_row_at(0)[i]) for i in range(5)]
            check("row shows user@host", f"{USER}@127.0.0.1:{PORT}" in first_row, str(first_row))

            print("\n[3] run a command from the panel and see its output and exit status")
            panel.query_one("#command", Button).press()
            await pilot.pause(0.5)
            check("run-command screen opened", isinstance(app.screen, RunCommandScreen),
                  type(app.screen).__name__)
            app.screen.query_one("#cmd", Input).value = "echo ui-command"
            app.screen.query_one("#run", Button).press()
            await pilot.pause(1.0)
            output = str(app.screen.query_one("#output").content)
            check("stdout shown", "ui-command" in output, output)
            check("exit status shown", "exit status: 0" in output, output)
            app.screen.query_one("#close", Button).press()
            await pilot.pause(0.4)

            print("\n[4] disconnecting SFTP leaves the SSH session alive")
            # The SSH terminal tab grabs focus on the bottom panel, and the
            # app disables pane/connect actions while a terminal is focused;
            # f10 is the one key that always returns to the panes.
            await pilot.press("f10")
            await pilot.pause(0.3)
            await connect_via_dialog(app, pilot, "sftp")
            check("sftp opened a file pane", app.left_pane.is_remote)
            check("sftp did not reuse the SSH connection",
                  app.left_pane.connection is not None
                  and app.left_pane.connection.session is not ssh_conn.session)
            await pilot.press("ctrl+d")
            await pilot.pause(0.8)
            check("sftp pane closed", not app.left_pane.is_remote)
            check(
                "the SSH session survived the SFTP disconnect",
                ssh_conn.session is not None and ssh_conn.session.is_connected,
            )
            check("the manager still owns the SSH session", len(get_manager().sessions()) == 1)

            print("\n[5] disconnecting the SSH session from the panel cleans up")
            await pilot.press("f11")
            await pilot.pause(0.5)
            panel = app.bottom_panel.connections
            panel.query_one("#disconnect", Button).press()
            await pilot.pause(1.0)
            check("manager is empty", len(get_manager().sessions()) == 0)
            check("terminal tab closed", not app.bottom_panel.has_shells)
            check("ssh-only tracking cleared", len(app._ssh_sessions) == 0)
    finally:
        server.close()
        await server.wait_closed()

    print(f"\n{'ALL SSH UI CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


asyncio.run(main())