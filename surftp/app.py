"""SURFTP application: two side-by-side file panes plus a global key map.

Layout, pane-focus routing, bindings and the connect flow live here; the panes
themselves stay protocol-blind (see ``surftp.widgets.pane``).

The connect flow is deliberately concentrated in this class rather than spread
across the dialogs: the dialogs collect input, ``surftp.net`` opens connections,
and only the app decides what gets written to the vault. A dialog that could
persist a credential by itself would make "did this save my password?"
unanswerable by reading any single file.
"""

from __future__ import annotations

from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Header

from surftp.bindings import BINDINGS
from surftp.fs import LocalFileSystem
from surftp.net import hostkeys
from surftp.net.connect import RemoteConnection, open_connection, resolve_credential
from surftp.net.types import (
    AuthMethod,
    ConnectionProfile,
    Credential,
    HostKeyUnknown,
    NetworkError,
    SecretKind,
)
from surftp.store import Vault, VaultLockedError, default_vault_path
from surftp.widgets import FilePane
from surftp.widgets.connect import ConnectDialog, ConnectRequest, ProfileListScreen
from surftp.widgets.dialogs import (
    HostKeyScreen,
    MasterPasswordScreen,
    MessageScreen,
    SecretPromptScreen,
)


class SurfFTPApp(App[None]):
    """The two-pane commander UI."""

    CSS_PATH = "app.tcss"
    TITLE = "SURFTP"
    BINDINGS = BINDINGS

    def __init__(self, vault_path: Path | None = None) -> None:
        """Create the app, optionally overriding the vault location (used by tests)."""
        super().__init__()
        self._vault_path = vault_path or default_vault_path()
        self._vault: Vault | None = None

    def compose(self) -> ComposeResult:
        """Yield the layout: header, two independent panes, footer."""
        start = str(Path.cwd())
        yield Header()
        with Horizontal(id="panes"):
            yield FilePane(LocalFileSystem(), start, id="left")
            yield FilePane(LocalFileSystem(), start, id="right")
        yield Footer()

    def on_mount(self) -> None:
        """Focus the left pane's table so the app is keyboard-usable immediately."""
        self.left_pane.table.focus()

    # ------------------------------------------------------------------
    # Pane routing
    # ------------------------------------------------------------------

    @property
    def left_pane(self) -> FilePane:
        """The left pane widget."""
        return self.query_one("#left", FilePane)

    @property
    def right_pane(self) -> FilePane:
        """The right pane widget."""
        return self.query_one("#right", FilePane)

    @property
    def active_pane(self) -> FilePane:
        """The pane that currently contains focus — every action routes through this."""
        if self.right_pane.has_focus_within:
            return self.right_pane
        return self.left_pane

    @property
    def inactive_pane(self) -> FilePane:
        """The pane without focus; the future transfer target."""
        return self.right_pane if self.active_pane is self.left_pane else self.left_pane

    # ------------------------------------------------------------------
    # Navigation actions
    # ------------------------------------------------------------------

    def action_focus_next_pane(self) -> None:
        """Move keyboard focus between the two panes."""
        if self.active_pane is self.left_pane:
            self.right_pane.table.focus()
        else:
            self.left_pane.table.focus()

    def action_open_selected(self) -> None:
        """Open the selected entry in the focused pane."""
        self.active_pane.open_selected()

    def action_go_parent(self) -> None:
        """Move the focused pane to its parent directory."""
        self.active_pane.go_parent()

    def action_refresh(self) -> None:
        """Reload the focused pane's listing."""
        self.active_pane.refresh_listing()

    def action_toggle_hidden(self) -> None:
        """Toggle dotfile visibility in the focused pane."""
        self.active_pane.show_hidden = not self.active_pane.show_hidden

    def action_toggle_help(self) -> None:
        """Show Textual's built-in help panel with the full key list."""
        self.action_show_help_panel()

    def action_quit(self) -> None:
        """Close any live connections and the vault, then exit."""
        self.close_everything()

    @work
    async def close_everything(self) -> None:
        """Tear down connections and lock the vault before the process ends."""
        for pane in (self.left_pane, self.right_pane):
            if pane.is_remote:
                await pane.detach_connection()
        if self._vault is not None:
            self._vault.close()
        self.exit()

    # ------------------------------------------------------------------
    # Vault
    # ------------------------------------------------------------------

    async def ensure_vault(self) -> Vault | None:
        """Return an unlocked vault, prompting for the master password if needed.

        Returns ``None`` when the user cancels — connecting without a vault is a
        perfectly good workflow (type the password, don't save it), so a
        cancelled unlock must not block the connection.
        """
        if self._vault is not None and self._vault.is_unlocked:
            return self._vault

        creating = not Vault.exists(self._vault_path)
        password = await self.push_screen_wait(MasterPasswordScreen(creating=creating))
        if password is None:
            return None
        try:
            if self._vault is None:
                self._vault = Vault.open_or_create(self._vault_path, password)
            else:
                self._vault.unlock(password)
        except VaultLockedError as exc:
            await self.push_screen_wait(MessageScreen("Vault", str(exc)))
            return None
        except (OSError, RuntimeError) as exc:
            await self.push_screen_wait(MessageScreen("Vault", f"Could not open the vault: {exc}"))
            return None
        return self._vault

    def action_lock_vault(self) -> None:
        """Forget the master password, so stored secrets need it again."""
        if self._vault is not None:
            self._vault.lock()
            self.notify("Vault locked.")

    # ------------------------------------------------------------------
    # Connect / disconnect
    # ------------------------------------------------------------------

    def action_connect(self) -> None:
        """Open the connect dialog for the focused pane."""
        self.connect_flow()

    @work
    async def connect_flow(self) -> None:
        """Run the full connect sequence: dialog -> credential -> connection -> save.

        Written as one worker so each step can await the one before it. Nothing
        here touches the vault unless the user ticked "Save to vault".
        """
        request = await self.push_screen_wait(ConnectDialog())
        if request is None:
            return

        vault = self._vault if (self._vault and self._vault.is_unlocked) else None
        if request.save_to_vault:
            vault = await self.ensure_vault()
            if vault is None:
                return  # they cancelled the unlock; don't connect-and-silently-not-save

        profile = request.profile
        if request.save_to_vault and vault is not None:
            profile = await self.persist_profile(vault, profile, request)

        await self.connect_profile(profile, vault, request.credential)

    async def persist_profile(
        self, vault: Vault, profile: ConnectionProfile, request: ConnectRequest
    ) -> ConnectionProfile:
        """Write the profile and its secrets to the vault, returning the saved profile.

        The profile row is written first so its id exists to key the secrets by —
        that id is also bound into each ciphertext as associated data.
        """
        profile_id = vault.save_profile(profile)
        saved = profile.with_id(profile_id)

        if request.credential.password:
            vault.put_secret(profile_id, SecretKind.PASSWORD, request.credential.password)
        if request.credential.key_passphrase:
            vault.put_secret(profile_id, SecretKind.KEY_PASSPHRASE, request.credential.key_passphrase)
        if saved.auth_method is AuthMethod.PEM_STORED and saved.pem_path:
            try:
                key_text = Path(saved.pem_path).read_text(encoding="utf-8")
            except OSError as exc:
                await self.push_screen_wait(
                    MessageScreen("Key not saved", f"Could not read {saved.pem_path}: {exc}")
                )
            else:
                vault.put_secret(profile_id, SecretKind.PRIVATE_KEY, key_text)
        return saved

    def action_open_profiles(self) -> None:
        """Show the saved-profile picker."""
        self.profiles_flow()

    @work
    async def profiles_flow(self) -> None:
        """List saved profiles and connect to whichever one the user picks.

        The list renders while the vault is locked; unlocking happens only if
        the chosen profile actually needs a stored secret.
        """
        if not Vault.exists(self._vault_path):
            await self.push_screen_wait(
                MessageScreen("Profiles", "No vault yet — connect with F9 and tick 'Save to vault'.")
            )
            return
        if self._vault is None:
            try:
                self._vault = Vault.open(self._vault_path)
            except (OSError, RuntimeError) as exc:
                await self.push_screen_wait(MessageScreen("Profiles", f"Could not open the vault: {exc}"))
                return

        profile = await self.push_screen_wait(ProfileListScreen(self._vault.list_profiles()))
        if profile is None:
            return

        vault = self._vault
        if profile.auth_method is not AuthMethod.AGENT and not vault.is_unlocked:
            vault = await self.ensure_vault() or vault
        await self.connect_profile(profile, vault if vault.is_unlocked else None, None)

    def on_profile_list_screen_delete_requested(
        self, message: ProfileListScreen.DeleteRequested
    ) -> None:
        """Delete a saved profile at the picker's request."""
        if self._vault is not None and message.profile.id is not None:
            self._vault.delete_profile(message.profile.id)
            self.notify(f"Deleted profile '{message.profile.name}'.")

    async def connect_profile(
        self,
        profile: ConnectionProfile,
        vault: Vault | None,
        typed: Credential | None,
    ) -> None:
        """Resolve credentials, open the connection, and attach it to the focused pane.

        Every failure mode gets its own message: an unknown host asks for trust
        and retries, a changed host key is a hard stop, and everything else lands
        in the pane's subtitle exactly as a local ``FileSystemError`` would.
        """
        pane = self.active_pane
        pane.border_subtitle = f"Connecting to {profile.display}…"
        try:
            credential = await resolve_credential(profile, vault, typed, self.ask_secret)
            connection = await open_connection(profile, credential)
        except HostKeyUnknown as unknown:
            if await self.offer_host_key(unknown):
                await self.connect_profile(profile, vault, typed)  # retry, now trusted
            else:
                pane.border_subtitle = "Connection cancelled: host key not trusted."
            return
        except NetworkError as exc:
            pane.border_subtitle = f"Error: {exc}"
            self.notify(str(exc), severity="error", timeout=10)
            return

        self.attach(pane, connection, vault)

    def attach(self, pane: FilePane, connection: RemoteConnection, vault: Vault | None) -> None:
        """Hand a live connection to a pane and record the successful use."""
        pane.attach_connection(connection)
        if vault is not None and connection.profile.id is not None:
            vault.touch_profile(connection.profile.id)
        self.notify(f"Connected to {connection.profile.display}")

    async def offer_host_key(self, unknown: HostKeyUnknown) -> bool:
        """Show the fingerprint prompt and, on acceptance, write it to known_hosts."""
        trusted = await self.push_screen_wait(
            HostKeyScreen(unknown.host, unknown.port, unknown.fingerprint, unknown.key_type)
        )
        if not trusted:
            return False
        key = await hostkeys.fetch_host_key(unknown.host, unknown.port)
        hostkeys.remember_host_key(unknown.host, unknown.port, key)
        return True

    async def ask_secret(self, prompt: str) -> str | None:
        """Prompt for a secret at connect time; the ``ask_secret`` hook for ``net``."""
        return await self.push_screen_wait(SecretPromptScreen(prompt))

    def action_disconnect(self) -> None:
        """Disconnect the focused pane and return it to the local disk."""
        self.disconnect_flow()

    @work
    async def disconnect_flow(self) -> None:
        """Close the focused pane's connection, if it has one."""
        pane = self.active_pane
        if not pane.is_remote:
            self.notify("This pane is already local.")
            return
        label = pane.filesystem.label
        await pane.detach_connection()
        self.notify(f"Disconnected from {label}")
