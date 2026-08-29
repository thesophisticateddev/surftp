"""Regression tests for session tabs.

Every check here corresponds to a bug that actually shipped and crashed or
misrouted: focusing a half-mounted pane, the right side stealing focus at
start-up, `tab` being swallowed by Textual's own focus-next, and a stale focus
event undoing a pane switch. They start a real SSH server, because opening a
session tab is exactly where the crash was.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

WORK = Path(tempfile.mkdtemp(prefix="surftp-sess-"))
os.environ["HOME"] = str(WORK / "home")
(WORK / "home").mkdir()

sys.path.insert(0, "/home/salman/Documents/surftp")

import asyncssh  # noqa: E402
from textual.widgets import Button, DataTable, Input  # noqa: E402

from surftp.app import SurfFTPApp  # noqa: E402
from surftp.widgets.connect import ConnectDialog  # noqa: E402
from surftp.widgets.dialogs import HostKeyScreen  # noqa: E402

PORT = 8035
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
    """Password-only test server."""

    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return username == USER and password == PASSWORD


async def connect(app: SurfFTPApp, pilot) -> None:
    """Drive the connect dialog through to an open session tab."""
    await pilot.press("f9")
    await pilot.pause(0.4)
    screen = app.screen
    assert isinstance(screen, ConnectDialog), type(screen)
    screen.query_one("#host", Input).value = "127.0.0.1"
    screen.query_one("#port", Input).value = str(PORT)
    screen.query_one("#username", Input).value = USER
    screen.query_one("#password", Input).value = PASSWORD
    screen.query_one("#remote_path", Input).value = str(SERVE)
    screen.query_one("#connect", Button).press()
    await pilot.pause(0.6)
    if isinstance(app.screen, HostKeyScreen):
        app.screen.query_one("#trust", Button).press()
    await pilot.pause(1.5)


async def main() -> None:
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.create_server(
        Server, "127.0.0.1", PORT, server_host_keys=[host_key], sftp_factory=True
    )
    app = SurfFTPApp(vault_path=WORK / "vault.duckdb")
    try:
        async with app.run_test() as pilot:
            await pilot.pause(0.5)

            print("\n[1] start-up: the LEFT side is active")
            # Both sides mount a Local tab and fire TabActivated; the right side
            # used to grab focus and the app started on the wrong side.
            check("active side is left", app._active_side == "left", app._active_side)
            check("left pane focused", isinstance(app.focused, DataTable))
            check("left pane is the local tab", app.left_pane.id == "left-0", str(app.left_pane.id))

            print("\n[2] tab switches sides, twice in a row, with no pause between")
            # `tab` was being consumed by Textual's focus-next, and a late focus
            # event could undo the switch. Both need back-to-back presses to work.
            await pilot.press("tab")
            first = app.active_pane
            await pilot.press("tab")
            second = app.active_pane
            check("first tab moved to the right pane", first is app.right_pane, str(first.id))
            check("second tab moved back to the left pane", second is app.left_pane, str(second.id))
            check("tab did not escape into another widget",
                  app.focused is app.left_pane.table, str(app.focused))

            print("\n[3] two keys in one burst hit the same pane")
            # tab then ctrl+h with no pause: the toggle must land on the pane the
            # tab just selected, not the one focus still pointed at.
            await pilot.press("tab")
            before = app.right_pane.table.row_count
            await pilot.press("ctrl+h")
            await pilot.pause(0.5)
            check("hidden toggle hit the right pane",
                  app.right_pane.table.row_count > before,
                  f"{before} -> {app.right_pane.table.row_count}")
            check("left pane untouched", not app.left_pane.show_hidden)
            await pilot.press("ctrl+h")
            await pilot.pause(0.4)
            await pilot.press("tab")
            await pilot.pause(0.2)

            print("\n[4] connecting opens a NEW tab and keeps Local alive")
            local_path = app.left_pane.path
            await connect(app, pilot)
            check("no crash opening the session tab", True)
            check("left side now has two tabs", len(app.left_sessions.panes) == 2,
                  str([p.id for p in app.left_sessions.panes]))
            check("the session tab is active", app.left_pane.is_remote, str(app.left_pane.id))
            check("session pane is ready (mounted)", app.left_pane.is_ready)
            names = [str(app.left_pane.table.get_row_at(i)[0])
                     for i in range(app.left_pane.table.row_count)]
            check("remote listing rendered", "remote-only.txt" in names, str(names))
            local_pane = app.left_sessions.panes[0]
            check("Local tab kept its path", local_pane.path == local_path, local_pane.path)
            check("Local tab is still local", not local_pane.is_remote)
            check("right side untouched", len(app.right_sessions.panes) == 1)

            print("\n[5] a second session opens a third tab")
            await connect(app, pilot)
            check("three tabs on the left", len(app.left_sessions.panes) == 3,
                  str([p.id for p in app.left_sessions.panes]))
            check("newest session is active", app.left_pane.id == "left-2", str(app.left_pane.id))

            print("\n[6] cycling between session tabs")
            await pilot.press("ctrl+pagedown")
            await pilot.pause(0.4)
            cycled = app.left_pane.id
            check("ctrl+pagedown changed tab", cycled != "left-2", cycled)
            check("focus follows to the table", isinstance(app.focused, DataTable), str(app.focused))
            await pilot.press("ctrl+pageup")
            await pilot.pause(0.4)
            check("ctrl+pageup came back", app.left_pane.id == "left-2", str(app.left_pane.id))

            print("\n[7] closing sessions returns to Local")
            await pilot.press("ctrl+d")
            await pilot.pause(0.8)
            check("one session closed", len(app.left_sessions.panes) == 2,
                  str([p.id for p in app.left_sessions.panes]))
            check("closing a session returns to Local",
                  not app.left_pane.is_remote, str(app.left_pane.id))
            # Closing activates Local, so reach the surviving session deliberately
            # rather than pressing ctrl+d again (which would target Local).
            await pilot.press("ctrl+pagedown")
            await pilot.pause(0.4)
            check("cycled onto the remaining session", app.left_pane.is_remote, str(app.left_pane.id))
            await pilot.press("ctrl+d")
            await pilot.pause(0.8)
            check("back to the Local tab only", len(app.left_sessions.panes) == 1,
                  str([p.id for p in app.left_sessions.panes]))
            check("left pane is local again", not app.left_pane.is_remote)
            check("Local kept its path", app.left_pane.path == local_path, app.left_pane.path)
            await pilot.press("ctrl+d")
            await pilot.pause(0.4)
            check("closing the Local tab is refused", len(app.left_sessions.panes) == 1)
    finally:
        server.close()
        await server.wait_closed()

    print(f"\n{'ALL SESSION CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


asyncio.run(main())
