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

import posixpath
from pathlib import Path

from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.widget import Widget
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
from surftp.shell.session import ShellSession
from surftp.store import Vault, VaultLockedError, default_vault_path
from surftp.transfer import ConflictPolicy, TransferEngine, plan_transfer
from surftp.widgets import FilePane
from surftp.widgets.bottom import BottomPanel
from surftp.widgets.connect import ConnectDialog, ConnectRequest, ProfileListScreen
from surftp.widgets.dialogs import (
    ConflictScreen,
    HostKeyScreen,
    MasterPasswordScreen,
    MessageScreen,
    SecretPromptScreen,
)
from surftp.widgets.sessions import SessionTabs
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
        # process can be killed when the tab closes or the app exits.
        self._shells: dict[str, ShellSession] = {}

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
                await self.connect_profile(profile, vault, typed)  # retry, now trusted
            else:
                pane.border_subtitle = "Connection cancelled: host key not trusted."
            return
        except NetworkError as exc:
            pane.border_subtitle = f"Error: {exc}"
            self.notify(str(exc), severity="error", timeout=10)
            return

        await self.attach(connection, vault)

    async def attach(self, connection: RemoteConnection, vault: Vault | None) -> None:
        """Open a new session tab for the connection and record the successful use."""
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
        closed = await sessions.close_active_session()
        if closed:
            # Kill the shell for the closed tab, if one was open.
            await self.close_shell(pane_id)
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

        items = await plan_transfer(source_fs, source_path, dest_path, entries=[entry])
        if not items:
            self.notify("Nothing to transfer.")
            return

        total_bytes = sum(item.size for item in items if not item.is_directory)
        panel = self.transfer_panel
        panel.start_job(total_bytes)
        for item in items:
            name = posixpath.basename(item.source_path)
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
        import posixpath

        future: asyncio.Future[ConflictPolicy] = asyncio.get_event_loop().create_future()
        name = posixpath.basename(item.source_path)

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
            self.call_from_thread(view.set_frame, message)
        # Shell ended; the tab stays until dismissed, showing a stale frame.

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


def _join_dest_path(base: str, name: str) -> str:
    """Join ``base`` and ``name`` using the appropriate path separator.

    If ``base`` looks like a Windows path (contains ``:\\``), use ``os.path``;
    otherwise use ``posixpath``. This is a heuristic — the engine does not
    know the destination's OS, so it guesses from the path syntax.
    """
    if ":\\" in base or base.startswith("\\\\"):
        import os.path

        return os.path.join(base, name)
    return posixpath.join(base, name)
