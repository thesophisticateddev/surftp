"""End-to-end: the app connecting to a live SSH server through the dialog."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

WORK = Path(tempfile.mkdtemp(prefix="surftp-uic-"))
os.environ["HOME"] = str(WORK / "home")
(WORK / "home").mkdir()

sys.path.insert(0, "/home/salman/Documents/surftp")

import asyncssh  # noqa: E402
from textual.widgets import Button, Input, Select  # noqa: E402

from surftp.app import SurfFTPApp  # noqa: E402
from surftp.widgets.connect import ConnectDialog  # noqa: E402
from surftp.widgets.dialogs import HostKeyScreen, MasterPasswordScreen  # noqa: E402

PORT = 8023
USER = "tester"
PASSWORD = "correct horse"
SERVE_ROOT = WORK / "served"
SERVE_ROOT.mkdir()
(SERVE_ROOT / "remote-only.txt").write_text("hello")
(SERVE_ROOT / "remote-dir").mkdir()

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


async def fill_dialog(app: SurfFTPApp, pilot, password: str, save: bool = False) -> None:
    """Open the connect dialog and fill it in for the test server."""
    await pilot.press("f9")
    await pilot.pause(0.3)
    screen = app.screen
    assert isinstance(screen, ConnectDialog), type(screen)
    screen.query_one("#host", Input).value = "127.0.0.1"
    screen.query_one("#port", Input).value = str(PORT)
    screen.query_one("#username", Input).value = USER
    screen.query_one("#password", Input).value = password
    screen.query_one("#remote_path", Input).value = str(SERVE_ROOT)
    screen.query_one("#save").value = save
    screen.query_one("#connect", Button).press()
    await pilot.pause(0.5)


async def main() -> None:
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.create_server(
        Server, "127.0.0.1", PORT, server_host_keys=[host_key], sftp_factory=True
    )
    app = SurfFTPApp(vault_path=WORK / "vault.duckdb")
    try:
        async with app.run_test() as pilot:
            # Session tabs mean "the left pane" is a different FilePane per tab:
            # connecting opens a NEW pane rather than mutating the existing one.
            # These read app.left_pane / app.right_pane at each point of use so
            # they assert on the pane the user is actually looking at.
            left = lambda: app.left_pane
            right = lambda: app.right_pane
            await pilot.pause(0.3)
            local_path = left().path

            print("\n[1] first connect prompts to trust the host key")
            await fill_dialog(app, pilot, PASSWORD)
            check("host key prompt shown", isinstance(app.screen, HostKeyScreen), type(app.screen).__name__)
            if isinstance(app.screen, HostKeyScreen):
                fp = str(app.screen.query_one(".fingerprint").content)
                check("fingerprint displayed", "SHA256:" in fp, fp)
                focused = app.screen.focused
                check(
                    "reject is the default focus",
                    focused is not None and focused.id == "reject",
                    str(focused),
                )
                app.screen.query_one("#trust", Button).press()
            await pilot.pause(1.2)

            print("\n[2] the pane is now remote and lists the server")
            check("pane is remote", left().is_remote)
            names = [str(left().table.get_row_at(i)[0]) for i in range(left().table.row_count)]
            check("remote listing rendered", "remote-only.txt" in names, str(names))
            check("directories first", names[:2] == ["..", "remote-dir"], str(names))
            check("title shows user@host", "127.0.0.1" in str(left().border_title), str(left().border_title))
            check("other pane stayed local", not right().is_remote and right().path == local_path)

            print("\n[3] the UI stays responsive while remote")
            await pilot.press("tab")
            check("tab still switches panes", app.active_pane is right())
            await pilot.press("tab")
            await pilot.press("enter")  # descend into remote-dir via ".."/cursor
            await pilot.pause(0.6)
            check("navigation works remotely", left().path != str(SERVE_ROOT), left().path)
            await pilot.press("ctrl+r")
            await pilot.pause(0.6)
            check("remote refresh works", "Error" not in str(left().border_subtitle), str(left().border_subtitle))

            print("\n[4] disconnect restores the local pane")
            await pilot.press("ctrl+d")
            await pilot.pause(0.8)
            check("pane is local again", not left().is_remote)
            check("returned to the previous local path", left().path == local_path, left().path)
            local_names = [str(left().table.get_row_at(i)[0]) for i in range(left().table.row_count)]
            check("local listing restored", "remote-only.txt" not in local_names)

            print("\n[5] a wrong password reports itself specifically")
            await fill_dialog(app, pilot, "wrong-password")
            await pilot.pause(1.2)
            subtitle = str(app.active_pane.border_subtitle)
            check("error names the password", "rejected the password" in subtitle, subtitle)
            check("pane stayed local after failure", not app.active_pane.is_remote)

            print("\n[6] ticking 'save to vault' asks for a master password first")
            await fill_dialog(app, pilot, PASSWORD, save=True)
            await pilot.pause(0.5)
            check("master password prompt", isinstance(app.screen, MasterPasswordScreen), type(app.screen).__name__)
            if isinstance(app.screen, MasterPasswordScreen):
                app.screen.query_one("#password", Input).value = "master-password"
                app.screen.query_one("#confirm", Input).value = "master-password"
                app.screen.query_one("#ok", Button).press()
            await pilot.pause(1.5)
            check("connected after saving", app.active_pane.is_remote, str(app.active_pane.border_subtitle))
            check("vault file created", (WORK / "vault.duckdb").exists())

            print("\n[7] the saved profile is in the picker, with its password")
            from surftp.net.types import SecretKind
            from surftp.store import Vault

            vault = Vault.open(WORK / "vault.duckdb")
            profiles = vault.list_profiles()
            check("profile saved", len(profiles) == 1 and profiles[0].host == "127.0.0.1", str(profiles))
            vault.unlock("master-password")
            pid = profiles[0].id
            assert pid is not None
            check("password stored encrypted", vault.get_secret(pid, SecretKind.PASSWORD) == PASSWORD)
            raw = vault._conn.execute("SELECT ciphertext FROM secrets WHERE profile_id = ?", [pid]).fetchone()
            check("ciphertext is not plaintext", raw is not None and PASSWORD.encode() not in bytes(raw[0]))
            vault.close()

            await pilot.press("ctrl+d")
            await pilot.pause(0.8)

            print("\n[8] reconnecting from the saved profile uses the stored password")
            from surftp.widgets.connect import ProfileListScreen

            await pilot.press("ctrl+o")
            await pilot.pause(0.6)
            check("picker opened", isinstance(app.screen, ProfileListScreen), type(app.screen).__name__)
            if isinstance(app.screen, ProfileListScreen):
                app.screen.query_one("#connect", Button).press()
            await pilot.pause(1.5)
            check(
                "connected with no password typed",
                app.active_pane.is_remote,
                str(app.active_pane.border_subtitle),
            )
            await pilot.press("ctrl+d")
            await pilot.pause(0.5)
    finally:
        server.close()
        await server.wait_closed()

    print(f"\n{'ALL UI CONNECT CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


asyncio.run(main())
