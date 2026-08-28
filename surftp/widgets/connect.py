"""The connect dialog and the saved-profile picker.

The dialog assembles a :class:`~surftp.net.types.ConnectionProfile` plus the
:class:`~surftp.net.types.Credential` typed alongside it. It does not connect
and it does not write to the vault — the app decides both, so a dialog can
never persist a password as a side effect of being filled in.

Two safety defaults are wired in here:

* **"Save to vault" starts unchecked.** A one-off connection must not silently
  leave a password on disk.
* **Plain FTP shows a clear-text warning** whenever TLS is off, both while
  choosing the protocol and when saving the password.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, OptionList, Select, Static
from textual.widgets.option_list import Option

from surftp.net.ssh import check_key_permissions
from surftp.net.types import (
    DEFAULT_PORTS,
    AuthMethod,
    ConnectionProfile,
    Credential,
    Protocol,
)


@dataclass(frozen=True, slots=True)
class ConnectRequest:
    """What the connect dialog hands back: where to go, how to prove identity, and whether to save."""

    profile: ConnectionProfile
    credential: Credential
    save_to_vault: bool


class ConnectDialog(ModalScreen[ConnectRequest | None]):
    """Collect a target host, an auth method, and the matching secret."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, profile: ConnectionProfile | None = None) -> None:
        """Open blank, or pre-filled from ``profile`` when editing a saved target."""
        super().__init__()
        self._profile = profile

    def compose(self) -> ComposeResult:
        """Yield the form: target fields, auth selector, and the conditional secret area."""
        p = self._profile
        with Vertical(id="dialog"):
            yield Label("Connect to a remote host", classes="title")
            with Horizontal(classes="row"):
                yield Select(
                    [(proto.value.upper(), proto.value) for proto in Protocol],
                    value=str(p.protocol) if p else Protocol.SFTP.value,
                    allow_blank=False,
                    id="protocol",
                )
                yield Input(placeholder="host", value=p.host if p else "", id="host")
                yield Input(
                    placeholder="port",
                    value=str(p.port) if p else str(DEFAULT_PORTS[Protocol.SFTP]),
                    id="port",
                )
            with Horizontal(classes="row"):
                yield Input(placeholder="username", value=p.username if p else "", id="username")
                yield Input(
                    placeholder="remote path (optional)",
                    value=(p.remote_path or "") if p else "",
                    id="remote_path",
                )
            yield Select(
                [
                    ("Password", AuthMethod.PASSWORD.value),
                    ("Private key file (.pem)", AuthMethod.PEM_FILE.value),
                    ("Private key stored in vault", AuthMethod.PEM_STORED.value),
                    ("ssh-agent", AuthMethod.AGENT.value),
                ],
                value=str(p.auth_method) if p else AuthMethod.PASSWORD.value,
                allow_blank=False,
                id="auth_method",
            )
            yield Input(placeholder="password", password=True, id="password")
            yield Input(
                placeholder="path to .pem private key",
                value=(p.pem_path or "") if p else "",
                id="pem_path",
                classes="hidden",
            )
            yield Input(
                placeholder="key passphrase (leave blank if unencrypted)",
                password=True,
                id="passphrase",
                classes="hidden",
            )
            yield Checkbox("Use TLS (FTPS)", value=p.use_tls if p else False, id="use_tls", classes="hidden")
            yield Static("", id="notice", classes="warning")
            with Horizontal(classes="row"):
                yield Input(
                    placeholder="save as (profile name)",
                    value=p.name if p else "",
                    id="name",
                )
                yield Checkbox("Save to vault", value=False, id="save")
            yield Static("", id="error", classes="error")
            with Horizontal(classes="buttons"):
                yield Button("Connect", variant="primary", id="connect")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        """Focus the host field and sync the conditional widgets to the initial state."""
        self.query_one("#host", Input).focus()
        self._sync_fields()

    def on_select_changed(self, event: Select.Changed) -> None:
        """Re-sync the form when the protocol or auth method changes.

        Changing the protocol also rewrites the port, but only while it still
        holds the old protocol's default — never clobber a port the user typed.
        """
        if event.select.id == "protocol":
            port_input = self.query_one("#port", Input)
            if port_input.value in {str(v) for v in DEFAULT_PORTS.values()}:
                port_input.value = str(DEFAULT_PORTS[Protocol(str(event.value))])
        self._sync_fields()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        """Re-evaluate the clear-text warning when TLS is toggled."""
        self._sync_fields()

    def _sync_fields(self) -> None:
        """Show only the inputs the chosen protocol and auth method actually use."""
        protocol = Protocol(str(self.query_one("#protocol", Select).value))
        method = AuthMethod(str(self.query_one("#auth_method", Select).value))

        # FTP has no key auth: force it back to password rather than offering a
        # combination that cannot work.
        if protocol is Protocol.FTP and method is not AuthMethod.PASSWORD:
            self.query_one("#auth_method", Select).value = AuthMethod.PASSWORD.value
            method = AuthMethod.PASSWORD

        self._set_visible("#password", method is AuthMethod.PASSWORD)
        self._set_visible("#pem_path", method is AuthMethod.PEM_FILE)
        self._set_visible("#passphrase", method in (AuthMethod.PEM_FILE, AuthMethod.PEM_STORED))
        self._set_visible("#use_tls", protocol is Protocol.FTP)

        self.query_one("#notice", Static).update(self._notice(protocol, method))

    def _notice(self, protocol: Protocol, method: AuthMethod) -> str:
        """Build the contextual warning line shown above the save controls."""
        if protocol is Protocol.FTP and not self.query_one("#use_tls", Checkbox).value:
            return (
                "Plain FTP sends your username and password in clear text over the network. "
                "Enable TLS unless you know the server does not support it."
            )
        if protocol is Protocol.SCP:
            return (
                "SCP transfers files but cannot list directories, so this pane browses over SFTP "
                "on the same connection. It will not work if the server has SFTP disabled."
            )
        if method is AuthMethod.PEM_STORED:
            return "The key file's contents will be copied into the vault and encrypted there."
        if method is AuthMethod.PEM_FILE:
            pem = self.query_one("#pem_path", Input).value
            return check_key_permissions(pem) or "" if pem else ""
        return ""

    def _set_visible(self, selector: str, visible: bool) -> None:
        """Show or hide one form widget by toggling the ``hidden`` class."""
        self.query_one(selector).set_class(not visible, "hidden")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Build the request on Connect, or dismiss on Cancel."""
        if event.button.id == "connect":
            self._submit()
        else:
            self.dismiss(None)

    def _submit(self) -> None:
        """Validate the form and dismiss with a :class:`ConnectRequest`."""
        error = self.query_one("#error", Static)
        host = self.query_one("#host", Input).value.strip()
        username = self.query_one("#username", Input).value.strip()
        if not host:
            error.update("Enter a host.")
            return
        if not username:
            error.update("Enter a username.")
            return
        try:
            port = int(self.query_one("#port", Input).value or 0)
        except ValueError:
            error.update("Port must be a number.")
            return

        protocol = Protocol(str(self.query_one("#protocol", Select).value))
        method = AuthMethod(str(self.query_one("#auth_method", Select).value))
        pem_path = self.query_one("#pem_path", Input).value.strip() or None
        if method in (AuthMethod.PEM_FILE, AuthMethod.PEM_STORED) and not pem_path:
            error.update("Choose a .pem private key file.")
            return

        name = self.query_one("#name", Input).value.strip() or f"{username}@{host}"
        profile = ConnectionProfile(
            id=self._profile.id if self._profile else None,
            name=name,
            protocol=protocol,
            host=host,
            port=port,
            username=username,
            auth_method=method,
            pem_path=pem_path,
            remote_path=self.query_one("#remote_path", Input).value.strip() or None,
            use_tls=self.query_one("#use_tls", Checkbox).value,
        )
        credential = Credential(
            password=self.query_one("#password", Input).value or None,
            key_passphrase=self.query_one("#passphrase", Input).value or None,
        )
        self.dismiss(
            ConnectRequest(
                profile=profile,
                credential=credential,
                save_to_vault=self.query_one("#save", Checkbox).value,
            )
        )

    def action_cancel(self) -> None:
        """Dismiss without connecting."""
        self.dismiss(None)


class ProfileListScreen(ModalScreen[ConnectionProfile | None]):
    """Pick a saved profile to connect to, or delete one.

    Rendering this list needs no unlock: profiles hold no secrets, which is
    exactly why the schema keeps them in a separate table.
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("delete", "delete_profile", "Delete"),
    ]

    def __init__(self, profiles: list[ConnectionProfile]) -> None:
        """Show ``profiles`` in the order the vault returned them (most recent first)."""
        super().__init__()
        self._profiles = profiles

    def compose(self) -> ComposeResult:
        """Yield the profile list, or an empty-state message."""
        with Vertical(id="dialog"):
            yield Label("Saved profiles", classes="title")
            if not self._profiles:
                yield Static("No saved profiles yet. Press F9 to connect to a host.", classes="warning")
            yield OptionList(
                *[
                    Option(f"{p.name}  —  {str(p.protocol).upper()}  {p.display}", id=str(p.id))
                    for p in self._profiles
                ],
                id="profiles",
            )
            yield Static("Enter connects · Delete removes · Esc cancels", classes="hint")
            with Horizontal(classes="buttons"):
                yield Button("Connect", variant="primary", id="connect")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        """Focus the list so arrow keys work immediately."""
        if self._profiles:
            self.query_one("#profiles", OptionList).focus()

    def _selected(self) -> ConnectionProfile | None:
        """Return the highlighted profile, or ``None`` when the list is empty."""
        option_list = self.query_one("#profiles", OptionList)
        index = option_list.highlighted
        if index is None or index >= len(self._profiles):
            return None
        return self._profiles[index]

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Enter on a row connects to it."""
        self.dismiss(self._selected())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Connect to the highlighted profile, or cancel."""
        self.dismiss(self._selected() if event.button.id == "connect" else None)

    class DeleteRequested(Message):
        """Emitted when the user asks to delete a saved profile."""

        def __init__(self, profile: ConnectionProfile) -> None:
            """Carry the profile the app should remove from the vault."""
            super().__init__()
            self.profile = profile

    def action_delete_profile(self) -> None:
        """Ask the app to delete the highlighted profile, and drop it from the list.

        Posted as a message rather than done here: this screen holds no vault
        reference, and handing it one would let a dialog mutate the store.
        """
        profile = self._selected()
        if profile is None or profile.id is None:
            return
        option_list = self.query_one("#profiles", OptionList)
        index = option_list.highlighted
        self.post_message(self.DeleteRequested(profile))
        if index is not None:
            option_list.remove_option_at_index(index)
            self._profiles.pop(index)

    def action_cancel(self) -> None:
        """Dismiss without choosing."""
        self.dismiss(None)
