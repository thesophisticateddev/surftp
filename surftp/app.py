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

from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.widget import Widget
from textual.widgets import Footer, Header

from surftp.bindings import BINDINGS
from surftp.fs import LocalFileSystem
from surftp.fs.types import display_name
from surftp.net import hostkeys
from surftp.net.connect import RemoteConnection, open_connection, resolve_credential
from surftp.net.credential_cache import get_credential_cache
from surftp.net.manager import get_manager
from surftp.net.types import (
    AuthMethod,
    ConnectionProfile,
    Credential,
    HostKeyUnknown,
    NetworkError,
    Protocol,
    SecretKind,
    key_passphrase_kind,
)
from surftp.shell.session import ShellSession
from surftp.store import Vault, VaultLockedError, default_vault_path
from surftp.transfer import ConflictPolicy, TransferEngine, plan_transfer
from surftp.widgets import FilePane
from surftp.widgets.bottom import BottomPanel
from surftp.widgets.connect import ConnectDialog, ConnectRequest, ProfileListScreen
from surftp.widgets.connections import ConnectionPanel
from surftp.widgets.dialogs import (
    ConflictScreen,
    HostKeyScreen,
    MasterPasswordScreen,
    MessageScreen,
    SecretPromptScreen,
)
from surftp.widgets.sessions import SessionTabs
from surftp.widgets.ssh_screens import ForwardsScreen, RunCommandScreen
from surftp.widgets.terminal import TerminalView
from surftp.widgets.transfers import TransferPanel


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
        # Which side the user is working on. Tracked explicitly rather than
        # derived from `self.focused` on demand: Textual applies a focus change
        # asynchronously, so two quick keypresses (tab then ctrl+h) would both
        # be handled while focus still reported the *old* side, sending the
        # second action to the wrong pane.
        self._active_side: str = "left"
        # The table we last asked to receive focus. Focus events that name a
        # different widget while this is pending are stale — they belong to an
        # earlier request that Textual delivered late — and must not undo the
        # side the user has since asked for.
        self._focus_intent: Widget | None = None
        self._current_engine: TransferEngine | None = None
        # Shells we have opened, keyed by the session pane's id, so the child
        # process can be killed when the tab closes or the app exits. SSH-only
        # sessions (no pane) are keyed by ``ssh-<session id>``.
        self._shells: dict[str, ShellSession] = {}
        # SSH sessions whose tab we opened but that own no file pane; keyed by
        # the same ``ssh-<session id>`` keys as ``_shells``.
        self._ssh_sessions: dict[str, RemoteConnection] = {}

    def compose(self) -> ComposeResult:
        """Yield the layout: header, two session tab groups, bottom panel, footer."""
        start = str(Path.cwd())
        yield Header()
        with Horizontal(id="panes"):
            yield SessionTabs("left", start, id="left-sessions")
            yield SessionTabs("right", start, id="right-sessions")
        yield BottomPanel(id="bottom")
        yield Footer()

    def on_mount(self) -> None:
        """Focus the left pane's table so the app is keyboard-usable immediately.

        Deferred: the Local tab is mounted by ``SessionTabs.on_mount``, which may
        not have run yet. ``focus_table`` reports readiness rather than raising,
        so a slow mount degrades to "not focused yet" instead of a crash.
        """
        self.call_after_refresh(lambda: self.left_pane.focus_table())

    # ------------------------------------------------------------------
    # Pane routing
    # ------------------------------------------------------------------

    @property
    def left_sessions(self) -> SessionTabs:
        """The left side's session tabs widget."""
        return self.query_one("#left-sessions", SessionTabs)

    @property
    def right_sessions(self) -> SessionTabs:
        """The right side's session tabs widget."""
        return self.query_one("#right-sessions", SessionTabs)

    @property
    def left_pane(self) -> FilePane:
        """The pane the user currently sees on the left — now the active session tab's."""
        return self.left_sessions.active_pane

    @property
    def right_pane(self) -> FilePane:
        """The pane the user currently sees on the right — now the active session tab's."""
        return self.right_sessions.active_pane

    @property
    def active_pane(self) -> FilePane:
        """The pane the user is working on — every action routes through this.

        Reads the tracked side rather than inspecting live focus, so an action
        dispatched in the same event-loop turn as a focus change still agrees
        with what the user just asked for. Focus keeps the side up to date via
        :meth:`on_descendant_focus`, which is what makes mouse clicks work too.
        """
        return self.left_pane if self._active_side == "left" else self.right_pane

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        """Follow focus into a pane, so clicking a pane also switches sides.

        Focus is applied asynchronously, so two quick pane switches deliver two
        events and the first can land after the second. A pending
        ``_focus_intent`` therefore wins: any event naming a different widget is
        a late echo of a superseded request and is dropped. With no intent
        pending the event is authoritative, which is what makes mouse clicks
        and Textual's own focus moves switch sides correctly.
        """
        if self._focus_intent is not None:
            if event.widget is not self._focus_intent:
                return
            self._focus_intent = None
        widget: Widget | None = event.widget
        while widget is not None:
            if isinstance(widget, SessionTabs):
                self._active_side = "right" if widget is self.right_sessions else "left"
                return
            widget = widget.parent

    @property
    def inactive_pane(self) -> FilePane:
        """The pane without focus; the future transfer target."""
        return self.right_pane if self.active_pane is self.left_pane else self.left_pane

    # ------------------------------------------------------------------
    # Navigation actions
    # ------------------------------------------------------------------

    def action_focus_next_pane(self) -> None:
        """Move keyboard focus between the two panes.

        The side is recorded *before* focusing, so the next action routes
        correctly even if Textual has not applied the focus change yet.
        """
        target = self.right_pane if self._active_side == "left" else self.left_pane
        self._active_side = "right" if self._active_side == "left" else "left"
        self._focus_intent = target.table if target.is_ready else None
        target.focus_table()

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
        await self.close_all_sessions()
        # SSH sessions that own no pane (terminals, tunnels) are not walked by
        # close_all_sessions; the manager closes them. The credential cache is
        # plaintext and its lifetime ends here.
        await get_manager().close_all()
        get_credential_cache().clear()
        if self._vault is not None:
            self._vault.close()
        self.exit()

    async def close_all_sessions(self) -> None:
        """Close every live connection and every shell on both sides.

        Walks all tabs, not just the visible pane: a background session tab
        still owns a real SSH connection, and missing one leaks it. Shell
        child processes are killed first — a leaked emulator is worse than a
        leaked connection because it survives the app.
        """
        for shell in list(self._shells.values()):
            await shell.close()
        self._shells.clear()
        self._ssh_sessions.clear()
        for sessions in (self._sessions_or_none("#left-sessions"),
                         self._sessions_or_none("#right-sessions")):
            if sessions is None:
                continue
            for pane in sessions.panes:
                if pane.is_remote:
                    await pane.close_connection()

    def _sessions_or_none(self, selector: str) -> SessionTabs | None:
        """Look up a side, tolerating a partly torn-down widget tree.

        Called during shutdown, when the children may already be gone; a failed
        lookup there means "nothing left to close", not an error.
        """
        try:
            return self.query_one(selector, SessionTabs)
        except NoMatches:
            return None

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
            # A locked vault must mean "forget everything": the in-memory
            # credential cache holds plaintext and dies with the vault's DEK.
            get_credential_cache().clear()
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

        await self.connect_profile(profile, vault, request.credential, open_shell=request.open_shell)

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
            # Written both to the single-key row (legacy) and the indexed row
            # for identity file 0, so both resolution paths find it.
            vault.put_secret(profile_id, SecretKind.KEY_PASSPHRASE, request.credential.key_passphrase)
            vault.put_secret(profile_id, key_passphrase_kind(0), request.credential.key_passphrase)
        for index, passphrase in enumerate(request.credential.key_passphrases):
            if passphrase:
                vault.put_secret(profile_id, key_passphrase_kind(index), passphrase)
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
        open_shell: bool = True,
    ) -> None:
        """Resolve credentials, open the connection, and open a new session tab.

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
                await self.connect_profile(profile, vault, typed, open_shell=open_shell)  # retry, now trusted
            else:
                pane.border_subtitle = "Connection cancelled: host key not trusted."
            return
        except NetworkError as exc:
            pane.border_subtitle = f"Error: {exc}"
            self.notify(str(exc), severity="error", timeout=10)
            return

        await self.attach(connection, vault, open_shell=open_shell)

    async def attach(
        self,
        connection: RemoteConnection,
        vault: Vault | None,
        *,
        open_shell: bool = True,
    ) -> None:
        """Open a session tab for the connection and record the successful use.

        An SSH-profile connection has no pane backend; it opens a terminal tab
        in the bottom panel instead and is listed on the Connections tab.
        """
        if connection.is_ssh_session:
            await self.attach_ssh(connection, vault, open_shell=open_shell)
            return
        # Determine which side to open the tab on
        sessions = self.left_sessions if self.active_pane is self.left_pane else self.right_sessions
        try:
            await sessions.open_session(connection)
        except RuntimeError as exc:
            self.notify(str(exc), severity="error", timeout=10)
            await connection.close()
            return
        if vault is not None and connection.profile.id is not None:
            vault.touch_profile(connection.profile.id)
        self._refresh_connections_panel()
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

    # ------------------------------------------------------------------
    # SSH sessions (no file pane: a terminal, a tunnel, or a command)
    # ------------------------------------------------------------------

    @staticmethod
    def _ssh_key(session) -> str:
        """The ``_shells``/``_ssh_sessions`` key for an SSH-only session."""
        return f"ssh-{id(session)}"

    async def attach_ssh(
        self,
        connection: RemoteConnection,
        vault: Vault | None,
        open_shell: bool,
    ) -> None:
        """Open an SSH session's terminal tab and list it on the Connections tab.

        An SSH profile owns no file pane; its connection is the whole point, so
        the connection manager keeps a reference for as long as the session
        lives. The terminal tab (or, with a ``remote_command``, the command's
        output) is where the user lands, and the Connections tab is where the
        session is managed.
        """
        session = connection.session
        assert session is not None
        profile = connection.profile
        key = self._ssh_key(session)
        label = profile.name if profile.name else profile.display
        self._ssh_sessions[key] = connection

        if profile.remote_command and not open_shell:
            # A stored remote command runs regardless of the shell toggle; the
            # toggle controls whether an interactive shell tab also opens.
            await self._open_ssh_shell(session, label, command=profile.remote_command)
        elif open_shell:
            await self._open_ssh_shell(
                session, label, command=profile.remote_command, pty=profile.request_pty
            )

        # Start the profile's auto-start forwards after authentication.
        if vault is not None and profile.id is not None and vault.is_unlocked:
            specs = vault.list_forwards(profile.id)
            failed = await session.start_auto_forwards(specs)
            for handle in failed:
                if handle.state == "failed":
                    self.notify(
                        f"Forward {handle.spec.description} failed: {handle.error}",
                        severity="warning",
                    )
        elif profile.id is not None:
            # Forward definitions live in the vault; if it is locked we simply
            # do not auto-start anything rather than block the connection.
            self.notify("Vault locked — auto-start forwards were not started.", severity="warning")

        if vault is not None and profile.id is not None:
            vault.touch_profile(profile.id)
        self._refresh_connections_panel()
        self.notify(f"Connected to {profile.display}")

    async def _open_ssh_shell(
        self,
        session,
        label: str,
        *,
        command: str | None = None,
        pty: bool = True,
    ) -> None:
        """Open one terminal tab on an SSH-only session, reusing the shell machinery."""
        key = self._ssh_key(session)
        if key in self._shells:
            return  # one terminal per session; the tab is already open
        view = TerminalView(id=f"view-{key}")
        try:
            shell = await ShellSession.start(
                session,
                cols=80,
                rows=24,
                command=command,
                request_pty=pty,
            )
        except Exception as exc:
            self.notify(f"Could not open a shell: {exc}", severity="error", timeout=10)
            return
        self._shells[key] = shell
        view.shell = shell
        tab_id = self.bottom_panel.add_shell(shell, label)
        self._read_shell(shell, view, tab_id, key)

    async def browse_sftp(self, session) -> None:
        """Open an SFTP pane on an existing SSH session ("browse this host").

        The reverse of today's flow: the connection already exists, so this
        creates the file pane lazily on it. The pane is granted its own
        reference so closing it does not take the SSH session down with it.
        """
        try:
            get_manager().retain(session)
            profile = session.profile
            filesystem = await session.start_sftp()
            path = await filesystem.realpath(profile.remote_path or ".")
        except Exception as exc:
            self.notify(f"Could not open SFTP on {session.profile.display}: {exc}", severity="error")
            return
        connection = RemoteConnection(profile, filesystem, path, session)
        await self.attach(connection, self._vault)

    def _refresh_connections_panel(self) -> None:
        """Re-render the Connections tab from the manager's live sessions."""
        try:
            self.bottom_panel.connections.refresh_connections(get_manager().sessions())
        except Exception:
            pass  # panel not mounted yet; harmless

    def _save_forward(self, profile_id: int, spec) -> int:
        """Persist one forward definition to the vault; the ForwardsScreen's on_save hook."""
        if self._vault is None or not self._vault.is_unlocked:
            return spec.id or 0
        return self._vault.save_forward(profile_id, spec)

    def _delete_forward(self, forward_id: int) -> None:
        """Remove one forward definition from the vault."""
        if self._vault is not None and self._vault.is_unlocked:
            self._vault.delete_forward(forward_id)

    def on_connection_panel_shell_requested(
        self, message: ConnectionPanel.ShellRequested
    ) -> None:
        """Open a terminal tab on the highlighted SSH session."""
        session = message.session
        label = session.profile.name or session.profile.display
        self._open_ssh_shell_worker(session, label)

    @work(exclusive=False)
    async def _open_ssh_shell_worker(self, session, label) -> None:
        """Worker wrapper around the shell-open coroutine for panel actions."""
        await self._open_ssh_shell(session, label)

    @work(exclusive=False)
    async def on_connection_panel_command_requested(
        self, message: ConnectionPanel.CommandRequested
    ) -> None:
        """Open the run-command view for the highlighted SSH session."""
        await self.push_screen_wait(RunCommandScreen(message.session))

    @work(exclusive=False)
    async def on_connection_panel_browse_requested(
        self, message: ConnectionPanel.BrowseRequested
    ) -> None:
        """Browse the highlighted session's SFTP from its existing connection."""
        await self.browse_sftp(message.session)

    @work(exclusive=False)
    async def on_connection_panel_forwards_requested(
        self, message: ConnectionPanel.ForwardsRequested
    ) -> None:
        """Open the forwards editor for the highlighted SSH session."""
        session = message.session
        specs: list = []
        if self._vault is not None and session.profile.id is not None and self._vault.is_unlocked:
            specs = self._vault.list_forwards(session.profile.id)
        await self.push_screen_wait(
            ForwardsScreen(
                session,
                specs,
                on_save=lambda spec: self._save_forward(session.profile.id, spec),
                on_delete=self._delete_forward,
            )
        )

    @work(exclusive=False)
    async def on_connection_panel_disconnect_requested(
        self, message: ConnectionPanel.DisconnectRequested
    ) -> None:
        """Close the terminal tab and release the highlighted session's connection."""
        await self.disconnect_ssh(message.session)

    async def disconnect_ssh(self, session) -> None:
        """Close an SSH session's terminal and release its connection reference.

        The connection closes only when nothing else holds it — an SFTP pane
        opened on the same session keeps it alive.
        """
        key = self._ssh_key(session)
        self._ssh_sessions.pop(key, None)
        await self.close_shell(key)
        profile = session.profile
        get_credential_cache().drop(profile)
        await get_manager().release(session)
        self._refresh_connections_panel()
        self.notify(f"Disconnected from {profile.display}")

    def action_disconnect(self) -> None:
        """Close the active session tab (disconnects it)."""
        self.disconnect_flow()

    @work
    async def disconnect_flow(self) -> None:
        """Close the active session tab, if it's not the Local tab."""
        sessions = self.left_sessions if self.active_pane is self.left_pane else self.right_sessions
        if sessions.active_pane is sessions._local_pane:
            self.notify("Cannot close the Local tab.")
            return
        
        # Check for active transfer
        if self._current_engine is not None:
            engine = self._current_engine
            active_fs = sessions.active_pane.filesystem
            if engine._source is active_fs or engine._destination is active_fs:
                self.notify("Cannot close tab while a transfer is in progress.", severity="warning")
                return
        
        pane = sessions.active_pane
        label = pane.filesystem.label
        pane_id = pane.id or ""
        connection = pane.connection  # capture before close clears it
        closed = await sessions.close_active_session()
        if closed:
            # Kill the shell for the closed tab, if one was open, and forget
            # any one-off secret cached for its profile.
            await self.close_shell(pane_id)
            if connection is not None:
                get_credential_cache().drop(connection.profile)
            self.notify(f"Disconnected from {label}")
        else:
            self.notify("Failed to close session tab.", severity="error")

    # ------------------------------------------------------------------
    # Transfers
    # ------------------------------------------------------------------

    @property
    def transfer_panel(self) -> TransferPanel:
        """The transfer queue panel (inside the bottom panel's Transfers tab)."""
        return self.bottom_panel.transfers

    @property
    def bottom_panel(self) -> BottomPanel:
        """The tabbed bottom panel (Transfers | Shell tabs)."""
        return self.query_one("#bottom", BottomPanel)

    def action_copy_to_other(self) -> None:
        """Copy the selected file(s) from the focused pane to the other pane."""
        self.transfer_flow(move=False)

    def action_move_to_other(self) -> None:
        """Move the selected file(s) from the focused pane to the other pane."""
        self.transfer_flow(move=True)

    @work
    async def transfer_flow(self, *, move: bool) -> None:
        """Run the full transfer sequence: plan → resolve conflicts → run engine.

        ``move=True`` deletes the source files after a successful copy.
        """
        source_pane = self.active_pane
        dest_pane = self.inactive_pane
        entry = source_pane.selected_entry()
        if entry is None or entry.name == "..":
            self.notify("Select a file or directory to transfer.")
            return

        source_fs = source_pane.filesystem
        dest_fs = dest_pane.filesystem
        source_path = entry.path
        # Destination is always the other pane's current directory.
        # The planner handles creating the directory/file inside it.
        dest_path = dest_pane.path

        items = await plan_transfer(source_fs, source_path, dest_fs, dest_path, entries=[entry])
        if not items:
            self.notify("Nothing to transfer.")
            return

        total_bytes = sum(item.size for item in items if not item.is_directory)
        panel = self.transfer_panel
        panel.start_job(total_bytes)
        for item in items:
            name = display_name(item.source_path)
            panel.add_item(item.source_path, name if not item.is_directory else f"{name}/", item.size)
        panel.visible = True

        from surftp.transfer.types import TransferJob

        job = TransferJob(
            items=items,
            total_bytes=total_bytes,
            source_label=source_fs.label,
            destination_label=dest_fs.label,
        )

        engine = TransferEngine(
            source_fs,
            dest_fs,
            on_progress=panel.update_progress,
            conflict_policy=ConflictPolicy.ASK,
            conflict_resolver=self._resolve_conflict,
        )
        self._current_engine = engine
        result = await engine.run(job)
        self._current_engine = None

        if result.completed > 0:
            dest_pane.refresh_listing()
            if move:
                await self._delete_source(source_fs, items)
                source_pane.refresh_listing()
        if result.failed > 0:
            self.notify(f"{result.failed} file(s) failed to transfer.", severity="warning")
        if result.completed > 0:
            self.notify(f"Transferred {result.completed} file(s).")

    async def _resolve_conflict(self, item) -> ConflictPolicy:
        """Push the conflict dialog and return the user's choice."""
        import asyncio
        future: asyncio.Future[ConflictPolicy] = asyncio.get_event_loop().create_future()
        name = display_name(item.source_path)

        def on_dismiss(policy: ConflictPolicy | None) -> None:
            if not future.done():
                future.set_result(policy or ConflictPolicy.SKIP)

        screen = ConflictScreen(name)
        screen.dismiss = on_dismiss  # type: ignore[assignment]
        await self.push_screen_async(screen)
        return await future

    async def _delete_source(self, source_fs, items) -> None:
        """Delete source files after a successful move."""
        from surftp.fs.types import FileSystemError

        for item in reversed(items):
            if not item.is_directory:
                try:
                    await source_fs.remove(item.source_path)
                except FileSystemError:
                    pass
        for item in items:
            if item.is_directory:
                try:
                    await source_fs.remove(item.source_path)
                except FileSystemError:
                    pass

    def action_toggle_transfers(self) -> None:
        """Show or hide the transfer queue panel."""
        panel = self.transfer_panel
        panel.visible = not panel.visible

    def action_cancel_transfer(self) -> None:
        """Cancel the running transfer, if any."""
        engine = getattr(self, "_current_engine", None)
        if engine is not None:
            engine.cancel()
            self.notify("Transfer cancelled.")
        else:
            self.notify("No transfer in progress.")

    # ------------------------------------------------------------------
    # Shells
    # ------------------------------------------------------------------

    def check_action(self, action: str, parameters) -> bool:
        """Disable pane/transfer actions while a shell has focus.

        The app's own bindings (``tab``, ``ctrl+d``, ``ctrl+w``, ``escape``,
        ``ctrl+pageup/pagedown``) are declared priority for Textual's focus
        handling — inside a shell those keys mean tab-completion, EOF,
        delete-word and the keys vim needs. Returning ``False`` here disables
        the binding so the raw key falls through to the focused terminal.

        ``f10`` and ``ctrl+q`` stay available: one key must always escape a
        shell, and quitting is quitting.
        """
        if isinstance(self.focused, TerminalView):
            if action in ("focus_panes", "quit"):
                return True
            return False
        return True

    def action_focus_panes(self) -> None:
        """Return focus to the active file pane — the one key that always escapes a shell."""
        self.active_pane.focus_table()
        self.bottom_panel.visible = True

    def action_connections(self) -> None:
        """Open the live-connections panel and refresh its rows."""
        self.bottom_panel.active = "connections"
        self._refresh_connections_panel()

    def on_tabbed_content_tab_activated(self, event) -> None:
        """Refresh the connections list whenever its tab is opened."""
        if event.tabbed_content.id == "bottom" and event.pane.id == "connections":
            self._refresh_connections_panel()

    def action_open_shell(self) -> None:
        """Open an interactive shell on the active session tab's connection."""
        self.shell_flow()

    @work
    async def shell_flow(self) -> None:
        """Open a shell for the active pane, refusing FTP sessions with a reason."""
        pane = self.active_pane
        if not pane.is_remote:
            self.notify("Connect a session tab first, then open a shell on it.")
            return
        connection = pane.connection
        if connection is None or connection.session is None:
            self.notify(
                "No shell is available on this session (FTP has no shell to run).",
                severity="warning",
            )
            return

        # One shell per session tab.
        if pane.id in self._shells:
            self.notify("This session already has a shell open.")
            return

        profile = connection.profile
        label = profile.name if profile.name else profile.display
        view = TerminalView(id=f"view-{pane.id}")
        try:
            shell = await ShellSession.start(
                connection.session,
                cols=80,
                rows=24,
            )
        except Exception as exc:
            self.notify(f"Could not open a shell: {exc}", severity="error", timeout=10)
            return

        self._shells[pane.id] = shell
        view.shell = shell
        tab_id = self.bottom_panel.add_shell(shell, label)
        # Start the thread-based frame reader for this shell.
        self._read_shell(shell, view, tab_id, pane.id)

    @work(thread=True, exclusive=False)
    async def _read_shell(
        self, shell: ShellSession, view: TerminalView, tab_id: str, pane_id: str
    ) -> None:
        """Poll the child pipe on a worker thread and post frames to the view.

        The pipe read is blocking; on the event loop it would starve the app.
        On this thread it just waits for the next frame (the child coalesces to
        at most one per 33 ms) and hands it back to the UI.
        """
        while not shell.exited:
            message = shell.next_message(timeout=0.1)
            if message is None:
                continue
            self.call_from_thread(self._set_shell_frame, tab_id, view, message)
        # Shell ended; the tab stays until dismissed, showing a stale frame.

    def _set_shell_frame(self, tab_id: str, fallback: TerminalView, message: dict) -> None:
        """Paint a frame onto the *displayed* terminal view for a shell tab.

        ``add_shell`` builds its own ``TerminalView`` inside the bottom panel;
        the view handed to ``_read_shell`` is only a fallback for the moment
        before that tab's pane is mounted. Painting the wrong view would show a
        permanently blank terminal.
        """
        displayed = self.bottom_panel.view_for(tab_id)
        target = displayed or fallback
        target.set_frame(message)

    async def close_shell(self, pane_id: str) -> None:
        """Close and kill the shell for a session pane, if one is open."""
        shell = self._shells.pop(pane_id, None)
        if shell is None:
            return
        try:
            # Remove the tab from the bottom panel if it exists.
            for tab_id, view in list(self.bottom_panel._shell_views.items()):
                if view.shell is shell:
                    self.bottom_panel.remove_shell(tab_id)
                    break
        except Exception:
            pass
        await shell.close()

    def action_close_session(self) -> None:
        """Close the active session tab (alias for ctrl+d)."""
        self.disconnect_flow()

    def action_next_session(self) -> None:
        """Switch to the next session tab on the focused side."""
        sessions = self.left_sessions if self.active_pane is self.left_pane else self.right_sessions
        sessions.next_session()

    def action_previous_session(self) -> None:
        """Switch to the previous session tab on the focused side."""
        sessions = self.left_sessions if self.active_pane is self.left_pane else self.right_sessions
        sessions.previous_session()

    def action_activate_session_1(self) -> None:
        """Jump to session tab 1 (Local)."""
        sessions = self.left_sessions if self.active_pane is self.left_pane else self.right_sessions
        sessions.activate(0)

    def action_activate_session_2(self) -> None:
        """Jump to session tab 2."""
        sessions = self.left_sessions if self.active_pane is self.left_pane else self.right_sessions
        sessions.activate(1)

    def action_activate_session_3(self) -> None:
        """Jump to session tab 3."""
        sessions = self.left_sessions if self.active_pane is self.left_pane else self.right_sessions
        sessions.activate(2)

    def action_activate_session_4(self) -> None:
        """Jump to session tab 4."""
        sessions = self.left_sessions if self.active_pane is self.left_pane else self.right_sessions
        sessions.activate(3)

    def action_activate_session_5(self) -> None:
        """Jump to session tab 5."""
        sessions = self.left_sessions if self.active_pane is self.left_pane else self.right_sessions
        sessions.activate(4)

    def action_activate_session_6(self) -> None:
        """Jump to session tab 6."""
        sessions = self.left_sessions if self.active_pane is self.left_pane else self.right_sessions
        sessions.activate(5)

    def action_activate_session_7(self) -> None:
        """Jump to session tab 7."""
        sessions = self.left_sessions if self.active_pane is self.left_pane else self.right_sessions
        sessions.activate(6)

    def action_activate_session_8(self) -> None:
        """Jump to session tab 8."""
        sessions = self.left_sessions if self.active_pane is self.left_pane else self.right_sessions
        sessions.activate(7)

    def action_activate_session_9(self) -> None:
        """Jump to session tab 9."""
        sessions = self.left_sessions if self.active_pane is self.left_pane else self.right_sessions
        sessions.activate(8)
