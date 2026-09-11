"""Modal screens for running a remote command and editing a session's forwards.

Both live behind the connection panel: a command needs no PTY and its output is
worth keeping (distinct from the interactive shell, whose output scrolls away),
and forwards need a place to be started, stopped and added without touching the
underlying connection.

The screens hold a reference to the ``SSHSession`` they act on; they never
touch the connection manager or the vault directly. Vault persistence of
forward definitions is handed in as callbacks so a screen cannot write the
store as a side effect of being rendered.
"""

from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Input, Label, Select, Static

from surftp.net.ssh import SSHSession
from surftp.net.types import ForwardKind, ForwardSpec


class RunCommandScreen(ModalScreen[None]):
    """Prompt for a command, run it without a PTY, and show stdout/stderr + exit status."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, session: SSHSession, initial: str | None = None) -> None:
        """Show the command prompt for ``session``, pre-filled with ``initial``."""
        super().__init__()
        self._session = session
        self._initial = initial or ""

    def compose(self) -> ComposeResult:
        """Yield the prompt, the output area, and the buttons."""
        with Vertical(id="dialog"):
            yield Label(f"Run command — {self._session.profile.display}", classes="title")
            yield Input(value=self._initial, placeholder="command", id="cmd")
            yield Static("", id="output", classes="output")
            with Horizontal(classes="buttons"):
                yield Button("Run", variant="primary", id="run")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        """Focus the command input."""
        self.query_one("#cmd", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Run the command, or dismiss."""
        if event.button.id == "run":
            self._start()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the prompt runs the command."""
        self._start()

    def _start(self) -> None:
        """Kick off the run on a worker so the modal stays responsive."""
        asyncio.create_task(self._run())

    async def _run(self) -> None:
        """Execute the command and render its output and exit status."""
        command = self.query_one("#cmd", Input).value.strip()
        output = self.query_one("#output", Static)
        if not command:
            output.update("Enter a command.")
            return
        output.update("Running…")
        status, out, err = await self._session.run_command(command)
        parts = [f"exit status: {status if status is not None else 'unknown'}"]
        if out:
            parts.append(f"[stdout]\n{out}")
        if err:
            parts.append(f"[stderr]\n{err}")
        output.update("\n".join(parts))

    def action_cancel(self) -> None:
        """Dismiss without running."""
        self.dismiss(None)


class ForwardsScreen(ModalScreen[None]):
    """List a session's forwards with their state; start, stop, add and delete them."""

    BINDINGS = [("escape", "close", "Close")]

    def __init__(
        self,
        session: SSHSession,
        specs: list[ForwardSpec],
        on_save: callable,  # (profile_id, ForwardSpec) -> int ; persists and returns the id
        on_delete: callable,  # (forward_id) -> None
    ) -> None:
        """Show ``specs`` (the profile's saved forwards) for ``session``.

        ``on_save`` and ``on_delete`` let the app own vault writes; the screen
        owns starting and stopping listeners on the session.
        """
        super().__init__()
        self._session = session
        self._specs: list[ForwardSpec] = list(specs)
        self._on_save = on_save
        self._on_delete = on_delete

    def compose(self) -> ComposeResult:
        """Yield the table, the add-forward form, and the action buttons."""
        with Vertical(id="dialog"):
            yield Label(f"Forwards — {self._session.profile.display}", classes="title")
            yield DataTable(id="fwd-table", cursor_type="row", zebra_stripes=True)
            with Horizontal(classes="row"):
                yield Select(
                    [
                        ("Local (-L)", ForwardKind.LOCAL.value),
                        ("Remote (-R)", ForwardKind.REMOTE.value),
                        ("Dynamic (-D)", ForwardKind.DYNAMIC.value),
                        ("Unix socket", ForwardKind.SOCKET.value),
                    ],
                    value=ForwardKind.LOCAL.value,
                    allow_blank=False,
                    id="fwd-kind",
                )
                yield Input(placeholder="listen port or socket path", id="fwd-listen")
                yield Input(placeholder="dest host", id="fwd-dest")
                yield Input(placeholder="dest port", id="fwd-dest-port")
                yield Checkbox("auto start", value=True, id="fwd-auto")
            with Horizontal(classes="buttons"):
                yield Button("Start", id="start")
                yield Button("Stop", id="stop")
                yield Button("Delete", id="delete")
                yield Button("Add", variant="primary", id="add")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        """Configure the table and render the current state."""
        table = self.query_one("#fwd-table", DataTable)
        table.add_columns("Kind", "Listen", "Dest", "State")
        self._row_keys: dict[str, ForwardSpec] = {}
        self._refresh()

    def _state_for(self, spec: ForwardSpec) -> str:
        """The live state of ``spec`` on this session: running, stopped, or why it failed."""
        for handle in self._session.forwards():
            if handle.spec.id == spec.id:
                if handle.state == "failed":
                    return f"failed: {handle.error}"
                return handle.state
        return "stopped"

    def _refresh(self) -> None:
        """Re-render the forwards table from the current specs and live handles."""
        table = self.query_one("#fwd-table", DataTable)
        table.clear()
        self._row_keys.clear()
        for spec in self._specs:
            key = f"fwd-{spec.id}"
            listen = spec.listen_path or f"{spec.listen_host}:{spec.listen_port}"
            if spec.kind is ForwardKind.DYNAMIC:
                dest = "SOCKS"
            elif spec.kind is ForwardKind.SOCKET:
                dest = spec.dest_path or ""
            else:
                dest = f"{spec.dest_host}:{spec.dest_port}"
            table.add_row(spec.kind.value, listen, dest, self._state_for(spec), key=key)
            self._row_keys[key] = spec

    def _selected(self) -> ForwardSpec | None:
        """The forward under the cursor, or ``None``."""
        table = self.query_one("#fwd-table", DataTable)
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        if row_key is None:
            return None
        return self._row_keys.get(str(row_key))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route the five buttons."""
        action = event.button.id
        if action == "close":
            self.dismiss(None)
            return
        spec = self._selected() if action in ("start", "stop", "delete") else None
        if spec is None and action in ("start", "stop", "delete"):
            self.notify("Select a forward first.")
            return
        if action == "start" and spec is not None:
            self._start(spec)
        elif action == "stop" and spec is not None:
            self._stop(spec)
        elif action == "delete" and spec is not None:
            self._delete(spec)
        elif action == "add":
            self._add()

    def _start(self, spec: ForwardSpec) -> None:
        """Start a saved forward on this session (or re-start a stopped one)."""
        asyncio.create_task(self._start_async(spec))

    async def _start_async(self, spec: ForwardSpec) -> None:
        handle = await self._session.start_forward(spec)
        if handle.state == "failed":
            self.notify(f"Forward {spec.description} failed: {handle.error}", severity="error")
        self._refresh()

    def _stop(self, spec: ForwardSpec) -> None:
        """Stop a running forward."""
        if spec.id is not None:
            self._session.stop_forward(spec.id)
            self._refresh()

    def _delete(self, spec: ForwardSpec) -> None:
        """Stop the forward and remove its row from the vault."""
        if spec.id is not None:
            self._session.stop_forward(spec.id)
            self._on_delete(spec.id)
            self._specs = [s for s in self._specs if s.id != spec.id]
            self._refresh()

    def _add(self) -> None:
        """Build a forward from the form, persist it, and start it if auto-start is set."""
        try:
            spec = self._build_spec()
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        if self._on_save is not None:
            forward_id = self._on_save(spec)
            spec = type(spec)(**{**vars(spec), "id": forward_id})
            self._specs.append(spec)
        asyncio.create_task(self._start_async(spec))

    def _build_spec(self) -> ForwardSpec:
        """Parse the add form into a ``ForwardSpec``, raising ``ValueError`` on bad input."""
        kind = ForwardKind(str(self.query_one("#fwd-kind", Select).value))
        auto = self.query_one("#fwd-auto", Checkbox).value
        listen = self.query_one("#fwd-listen", Input).value.strip()
        dest = self.query_one("#fwd-dest", Input).value.strip()
        dest_port_raw = self.query_one("#fwd-dest-port", Input).value.strip()

        if not listen:
            raise ValueError("Enter a listen port (or socket path).")

        if kind is ForwardKind.DYNAMIC:
            try:
                port = int(listen)
            except ValueError as exc:
                raise ValueError("Dynamic forwards need a numeric listen port.") from exc
            return ForwardSpec(kind=kind, listen_port=port, auto_start=auto)

        if kind is ForwardKind.SOCKET:
            if not dest:
                raise ValueError("Enter the remote socket path.")
            return ForwardSpec(kind=kind, listen_path=listen, dest_path=dest, auto_start=auto)

        if not dest or not dest_port_raw:
            raise ValueError("Local and remote forwards need a destination host and port.")
        try:
            listen_port = int(listen)
            dest_port = int(dest_port_raw)
        except ValueError as exc:
            raise ValueError("Ports must be numbers.") from exc
        return ForwardSpec(
            kind=kind,
            listen_port=listen_port,
            dest_host=dest,
            dest_port=dest_port,
            auto_start=auto,
        )

    def action_close(self) -> None:
        """Close the screen."""
        self.dismiss(None)