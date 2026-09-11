"""The connection panel: lists live SSH connections and drives their lifecycle.

One tab in the bottom panel (beside Transfers and Shells). It answers the
question "what am I connected to?" — each row is a live connection from the
manager, with its auth method, uptime, channel count and forwards — and it is
where an SSH session (which has no file pane) is managed: open a shell, run a
command, browse its SFTP on demand, or disconnect.

The panel holds no session state of its own: the rows are re-read from
``SSHConnectionManager.sessions()`` whenever the app refreshes it, so a
connection that dies simply disappears from the list.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, DataTable, Static

from surftp.net.ssh import SSHSession


def _uptime(seconds: float) -> str:
    """Render an uptime as ``1h23m`` / ``4m12s`` / ``8s``."""
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"
    if seconds >= 60:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds}s"


class ConnectionPanel(Widget):
    """The live-connections tab: a table plus per-row actions."""

    class ShellRequested(Message):
        """Open a shell tab on the highlighted session."""

        def __init__(self, session: SSHSession) -> None:
            super().__init__()
            self.session = session

    class CommandRequested(Message):
        """Open the run-command view for the highlighted session."""

        def __init__(self, session: SSHSession) -> None:
            super().__init__()
            self.session = session

    class BrowseRequested(Message):
        """Open an SFTP pane on the highlighted session (browse this host)."""

        def __init__(self, session: SSHSession) -> None:
            super().__init__()
            self.session = session

    class ForwardsRequested(Message):
        """Open the forwards editor for the highlighted session."""

        def __init__(self, session: SSHSession) -> None:
            super().__init__()
            self.session = session

    class DisconnectRequested(Message):
        """Release the highlighted session's connection."""

        def __init__(self, session: SSHSession) -> None:
            super().__init__()
            self.session = session

    def compose(self) -> ComposeResult:
        """Yield the table of connections and the action buttons."""
        yield DataTable(id="conn-table", cursor_type="row", zebra_stripes=True)
        yield Static(
            "Highlight a connection, then Shell / Command / Browse SFTP / Forwards / Disconnect.",
            classes="hint",
        )
        with Horizontal(id="conn-actions"):
            yield Button("Shell", id="shell")
            yield Button("Command", id="command")
            yield Button("Browse SFTP", id="browse")
            yield Button("Forwards", id="forwards")
            yield Button("Disconnect", variant="error", id="disconnect")

    def on_mount(self) -> None:
        """Configure the table columns."""
        table = self.query_one("#conn-table", DataTable)
        table.add_columns("Host", "Auth", "Up", "Ch", "Forwards")
        self._rows: dict[str, SSHSession] = {}

    @property
    def table(self) -> DataTable:
        """The connections table; safe to call only after mount."""
        return self.query_one("#conn-table", DataTable)

    def refresh_connections(self, sessions: list[SSHSession]) -> None:
        """Re-render the table from the manager's live sessions."""
        table = self.query_one("#conn-table", DataTable)
        table.clear()
        self._rows.clear()
        for session in sessions:
            profile = session.profile
            fwd = session.forwards()
            running = sum(1 for h in fwd if h.state == "running")
            key = f"conn-{id(session)}"
            table.add_row(
                profile.display,
                str(profile.auth_method.value),
                _uptime(session.uptime_seconds) if session.is_connected else "dead",
                str(session.channel_count),
                f"{running}/{len(fwd)}",
                key=key,
            )
            self._rows[key] = session
        if self._rows:
            table.move_cursor(row=0)

    def _selected(self) -> SSHSession | None:
        """The session under the cursor, or the first row when none is highlighted."""
        table = self.query_one("#conn-table", DataTable)
        if not self._rows:
            return None
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        if row_key is None:
            return next(iter(self._rows.values()))
        return self._rows.get(str(row_key)) or next(iter(self._rows.values()))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route each action button to the highlighted session."""
        session = self._selected()
        if session is None:
            self.notify("Select a connection first.")
            return
        action = event.button.id
        if action == "shell":
            self.post_message(self.ShellRequested(session))
        elif action == "command":
            self.post_message(self.CommandRequested(session))
        elif action == "browse":
            self.post_message(self.BrowseRequested(session))
        elif action == "forwards":
            self.post_message(self.ForwardsRequested(session))
        elif action == "disconnect":
            self.post_message(self.DisconnectRequested(session))