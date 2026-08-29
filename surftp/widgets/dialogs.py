"""Modal screens for unlocking the vault, trusting a host, prompting for a secret, and resolving transfer conflicts.

Every screen here returns its result through ``dismiss``, so callers await a
value instead of wiring callbacks. Password inputs are always ``password=True``
— a credential must never be readable over the user's shoulder or captured in a
terminal screenshot.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from surftp.transfer.types import ConflictPolicy


class MasterPasswordScreen(ModalScreen[str | None]):
    """Ask for the vault master password, or set one up on first run.

    In ``creating`` mode this screen states plainly that a lost master password
    means a lost vault. That warning belongs here, at the moment of the
    decision, not in documentation the user will never read.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, *, creating: bool) -> None:
        """Prompt for an existing password, or create one when ``creating``."""
        super().__init__()
        self._creating = creating

    def compose(self) -> ComposeResult:
        """Yield the prompt, the password field(s), and the buttons."""
        with Vertical(id="dialog"):
            if self._creating:
                yield Label("Create a master password for the credential vault", classes="title")
                yield Static(
                    "This password encrypts every saved credential. It is never stored, and "
                    "there is NO recovery: if you lose it, the vault is unreadable. "
                    "Save it in a password manager now.",
                    classes="warning",
                )
                yield Input(placeholder="Master password", password=True, id="password")
                yield Input(placeholder="Confirm master password", password=True, id="confirm")
            else:
                yield Label("Unlock the credential vault", classes="title")
                yield Input(placeholder="Master password", password=True, id="password")
            yield Static("", id="error", classes="error")
            with Horizontal(classes="buttons"):
                yield Button("OK", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        """Focus the password field so the user can type immediately."""
        self.query_one("#password", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route the two buttons to submit or cancel."""
        if event.button.id == "ok":
            self._submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Treat Enter in either field as pressing OK."""
        self._submit()

    def _submit(self) -> None:
        """Validate the entry and dismiss with the password, or show why not."""
        password = self.query_one("#password", Input).value
        error = self.query_one("#error", Static)
        if not password:
            error.update("Enter a password.")
            return
        if self._creating:
            if password != self.query_one("#confirm", Input).value:
                error.update("The two passwords do not match.")
                return
            if len(password) < 8:
                error.update("Use at least 8 characters — this key protects every credential.")
                return
        self.dismiss(password)

    def action_cancel(self) -> None:
        """Dismiss without a password."""
        self.dismiss(None)


class SecretPromptScreen(ModalScreen[str | None]):
    """Ask for a one-off secret (a password or a key passphrase) at connect time."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, prompt: str) -> None:
        """Show ``prompt`` above a masked input."""
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        """Yield the prompt label, the masked field, and the buttons."""
        with Vertical(id="dialog"):
            yield Label(self._prompt, classes="title")
            yield Input(password=True, id="secret")
            with Horizontal(classes="buttons"):
                yield Button("OK", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        """Focus the input."""
        self.query_one("#secret", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Dismiss with the typed secret, or with ``None`` on cancel."""
        if event.button.id == "ok":
            self.dismiss(self.query_one("#secret", Input).value)
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter submits the secret."""
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        """Dismiss without a secret."""
        self.dismiss(None)


class HostKeyScreen(ModalScreen[bool]):
    """Show an unknown host's fingerprint and ask whether to trust it.

    Accepting is a deliberate click or keypress on a button that is **not**
    focused by default: the safe action must be the one that happens if the
    user hits Enter without reading.
    """

    BINDINGS = [("escape", "reject", "Reject")]

    def __init__(self, host: str, port: int, fingerprint: str, key_type: str) -> None:
        """Display the key details the user needs to compare out of band."""
        super().__init__()
        self._host = host
        self._port = port
        self._fingerprint = fingerprint
        self._key_type = key_type

    def compose(self) -> ComposeResult:
        """Yield the fingerprint block and the trust/reject buttons."""
        with Vertical(id="dialog"):
            yield Label(f"Unknown host: {self._host}:{self._port}", classes="title")
            yield Static(
                "SURFTP has never connected to this host. Verify the fingerprint below against "
                "one you obtained from the server's owner — not from this screen.",
                classes="warning",
            )
            yield Static(f"{self._key_type}\n{self._fingerprint}", classes="fingerprint")
            with Horizontal(classes="buttons"):
                yield Button("Reject", variant="primary", id="reject")
                yield Button("Trust and add to known_hosts", variant="warning", id="trust")

    def on_mount(self) -> None:
        """Focus Reject, so an unread Enter is the safe answer."""
        self.query_one("#reject", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Return ``True`` only when the user explicitly chose to trust."""
        self.dismiss(event.button.id == "trust")

    def action_reject(self) -> None:
        """Escape rejects the key."""
        self.dismiss(False)


class MessageScreen(ModalScreen[None]):
    """A dismissible message, used for hard failures such as a changed host key."""

    BINDINGS = [("escape", "close", "Close"), ("enter", "close", "Close")]

    def __init__(self, title: str, body: str) -> None:
        """Show ``title`` above ``body`` with a single Close button."""
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        """Yield the message and its Close button."""
        with Vertical(id="dialog"):
            yield Label(self._title, classes="title")
            yield Static(self._body, classes="warning")
            with Horizontal(classes="buttons"):
                yield Button("Close", variant="primary", id="close")

    def on_mount(self) -> None:
        """Focus the only button."""
        self.query_one("#close", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Close the message."""
        self.dismiss(None)

    def action_close(self) -> None:
        """Close the message."""
        self.dismiss(None)


class ConflictScreen(ModalScreen[ConflictPolicy]):
    """Ask what to do when a destination file already exists.

    Offers Overwrite / Overwrite all / Skip / Skip all / Rename, with **Skip
    focused by default** — the same reasoning as the host-key dialog: the
    non-destructive option is what an unread Enter must choose.
    """

    BINDINGS = [("escape", "skip", "Skip")]

    def __init__(self, filename: str) -> None:
        """Show the conflict dialog for ``filename``."""
        super().__init__()
        self._filename = filename

    def compose(self) -> ComposeResult:
        """Yield the prompt and the five buttons."""
        with Vertical(id="dialog"):
            yield Label(f"File already exists: {self._filename}", classes="title")
            yield Static(
                "Choose what to do. Skip is the safe default — it leaves the existing file untouched.",
                classes="warning",
            )
            with Horizontal(classes="buttons"):
                yield Button("Skip", variant="primary", id="skip")
                yield Button("Skip all", id="skip_all")
                yield Button("Overwrite", variant="warning", id="overwrite")
                yield Button("Overwrite all", id="overwrite_all")
                yield Button("Rename", id="rename")

    def on_mount(self) -> None:
        """Focus Skip, so an unread Enter is the safe answer."""
        self.query_one("#skip", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Return the chosen policy."""
        match event.button.id:
            case "skip":
                self.dismiss(ConflictPolicy.SKIP)
            case "skip_all":
                self.dismiss(ConflictPolicy.SKIP)  # app interprets as "all"
            case "overwrite":
                self.dismiss(ConflictPolicy.OVERWRITE)
            case "overwrite_all":
                self.dismiss(ConflictPolicy.OVERWRITE)  # app interprets as "all"
            case "rename":
                self.dismiss(ConflictPolicy.RENAME)
            case _:
                self.dismiss(ConflictPolicy.SKIP)

    def action_skip(self) -> None:
        """Escape skips this file."""
        self.dismiss(ConflictPolicy.SKIP)
