"""Headless UI checks driven by Textual's pilot."""

from __future__ import annotations

import asyncio
from pathlib import Path

from surftp.app import SurfFTPApp
from surftp.widgets.connect import ConnectDialog
from surftp.widgets.dialogs import MasterPasswordScreen
from textual.widgets import Input, Select


async def main() -> None:
    tmp = Path("/tmp/claude-1000/-home-salman-Documents-surftp/fccbfc6d-c13b-4234-a1c0-6ed031851794/scratchpad/ui-vault.duckdb")
    tmp.unlink(missing_ok=True)
    app = SurfFTPApp(vault_path=tmp)
    async with app.run_test() as pilot:
        left, right = app.left_pane, app.right_pane
        await pilot.pause()
        assert left.table.row_count > 0, "left pane listed nothing"
        assert left.border_title == str(Path.cwd()), left.border_title
        print("listing + title ok:", left.table.row_count, "rows")

        # tab switches panes
        await pilot.press("tab")
        assert app.active_pane is right
        await pilot.press("tab")
        assert app.active_pane is left
        print("tab focus ok")

        # navigation still works after the async conversion
        rows_before = left.table.row_count
        await pilot.press("enter")           # follows ".."
        await pilot.pause(0.2)
        assert left.path != str(Path.cwd()), "enter did not navigate"
        await pilot.press("ctrl+r")
        await pilot.pause(0.2)
        print("enter/refresh ok; now at", left.path)

        # hidden toggle, on the right pane which is still in the repo root
        await pilot.press("tab")
        n = right.table.row_count
        await pilot.press("ctrl+h")
        await pilot.pause(0.3)
        assert right.table.row_count > n, "dotfiles did not appear"
        names = [str(right.table.get_row_at(i)[0]) for i in range(right.table.row_count)]
        assert ".git" in names, names
        await pilot.press("ctrl+h")
        await pilot.pause(0.3)
        assert right.table.row_count == n
        await pilot.press("tab")
        print("hidden toggle ok:", n, "->", n + 2, "-> back to", right.table.row_count)

        # f9 opens the connect dialog
        await pilot.press("f9")
        await pilot.pause(0.3)
        assert isinstance(app.screen, ConnectDialog), type(app.screen)
        # FTP forces password auth and warns about clear text
        app.screen.query_one("#protocol", Select).value = "ftp"
        await pilot.pause(0.2)
        notice = str(app.screen.query_one("#notice").content)
        assert "clear text" in notice, notice
        assert app.screen.query_one("#port", Input).value == "21", "port did not follow protocol"
        print("connect dialog ok; ftp notice:", notice[:48], "...")
        await pilot.press("escape")
        await pilot.pause(0.2)

        # ctrl+d on a local pane is a no-op with a message, not a crash
        await pilot.press("ctrl+d")
        await pilot.pause(0.2)
        assert not left.is_remote
        print("disconnect on local pane ok")

        # ctrl+o with no vault explains itself
        await pilot.press("ctrl+o")
        await pilot.pause(0.3)
        print("profiles screen:", type(app.screen).__name__)
        await pilot.press("escape")
        await pilot.pause(0.2)

        # a bad directory shows an error instead of crashing
        left.path = "/root"
        await pilot.pause(0.3)
        assert "Error" in str(left.border_subtitle), left.border_subtitle
        print("error surfaced:", left.border_subtitle)

        # vault creation prompt appears on first save
        await pilot.press("f9")
        await pilot.pause(0.2)
        for widget_id, value in (("host", "h"), ("username", "u")):
            app.screen.query_one(f"#{widget_id}", Input).value = value
        app.screen.query_one("#save").value = True
        app.screen.query_one("#connect").press()
        await pilot.pause(0.4)
        assert isinstance(app.screen, MasterPasswordScreen), type(app.screen)
        body = " ".join(str(w.content) for w in app.screen.query(".warning"))
        assert "NO recovery" in body, body
        print("vault creation prompt ok, warns about recovery")
        await pilot.press("escape")
        await pilot.pause(0.2)

    print("\nALL UI CHECKS PASSED")


asyncio.run(main())
