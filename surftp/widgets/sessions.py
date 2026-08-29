"""Session tabs widget: manages multiple FilePane instances per side.

Each side of the commander view has a SessionTabs widget that owns the panes
for that side. Tab 1 is always "Local" and cannot be closed. Remote connections
open new tabs, and the user switches between them instead of losing the local
view every time they connect.

The app asks SessionTabs which pane is active and never reaches past it into
the tab machinery. This is the compatibility contract that makes the feature
safe: everything downstream (active_pane, inactive_pane, every action_*, the
connect flow, the transfer flow, close_everything) continues to work unmodified
because all of it is already written in terms of those two properties.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import TabbedContent, TabPane

from surftp.fs import LocalFileSystem
from surftp.net.connect import RemoteConnection
from surftp.widgets.pane import FilePane

MAX_SESSIONS_PER_SIDE = 8


class SessionTabs(Vertical):
    """One side of the commander view: a tab bar over a stack of FilePanes.

    Owns the panes for its side. The app asks it which pane is active and never
    reaches past it into the tab machinery.
    """

    def __init__(self, side: str, initial_path: str, **kwargs) -> None:
        """Create a session tabs widget for one side.

        ``side`` is "left" or "right", used for generating unique pane ids.
        ``initial_path`` is the starting directory for the Local tab.
        """
        super().__init__(**kwargs)
        self.side = side
        self._initial_path = initial_path
        self._tabbed: TabbedContent | None = None
        self._panes: list[FilePane] = []
        self._tab_ids: list[str] = []  # TabPane ids corresponding to each pane
        self._local_pane: FilePane | None = None
        self._next_id = 0

    def compose(self) -> ComposeResult:
        """Yield the tabbed content widget."""
        yield TabbedContent(id=f"{self.side}-tabs")

    def on_mount(self) -> None:
        """Create the Local tab.

        Deliberately *not* async: awaiting ``add_pane`` from inside this widget's
        own ``on_mount`` deadlocks, because the mount it waits on is queued
        behind the handler doing the waiting. Nothing here needs the pane to be
        ready synchronously — every caller that focuses it goes through
        :meth:`_focus_when_ready`, which retries after a refresh.
        """
        self._tabbed = self.query_one(TabbedContent)
        # Make the Tabs widget non-focusable so tab key goes to the table
        tabs = self._tabbed.query_one("Tabs")
        if tabs:
            tabs.can_focus = False
        self._local_pane = self._create_pane(LocalFileSystem(), self._initial_path, "Local")
        tab_id = "local"
        local_tab = TabPane("Local", self._local_pane, id=tab_id)
        self._panes.append(self._local_pane)
        self._tab_ids.append(tab_id)
        self._tabbed.add_pane(local_tab)

    def _create_pane(self, filesystem, path: str, label: str) -> FilePane:
        """Create a new FilePane with a unique id."""
        pane_id = f"{self.side}-{self._next_id}"
        self._next_id += 1
        return FilePane(filesystem, path, id=pane_id)

    @property
    def active_pane(self) -> FilePane:
        """The pane the user currently sees on this side."""
        if self._tabbed is None:
            assert self._local_pane is not None
            return self._local_pane
        active_tab_id = self._tabbed.active
        for i, tab_id in enumerate(self._tab_ids):
            if tab_id == active_tab_id:
                return self._panes[i]
        # Fallback to local pane if something goes wrong
        assert self._local_pane is not None
        return self._local_pane

    @property
    def panes(self) -> list[FilePane]:
        """All panes on this side, for shutdown and transfer checks."""
        return list(self._panes)

    @property
    def session_count(self) -> int:
        """Number of open session tabs (excluding Local)."""
        return len(self._panes) - 1

    async def open_session(self, connection: RemoteConnection) -> FilePane:
        """Open a new tab for a remote connection and activate it.

        Returns the new pane. Raises RuntimeError if the session limit is reached.
        """
        if self.session_count >= MAX_SESSIONS_PER_SIDE:
            raise RuntimeError(
                f"Maximum of {MAX_SESSIONS_PER_SIDE} sessions per side reached. "
                "Close an existing session before opening a new one."
            )

        assert self._tabbed is not None
        profile = connection.profile
        label = profile.name if profile.name else profile.display
        # Truncate long labels to prevent tab bar scrolling
        if len(label) > 18:
            label = label[:15] + "..."

        pane = self._create_pane(connection.filesystem, connection.initial_path, label)
        pane._connection = connection  # Mark as remote
        tab_id = pane.id or f"{self.side}-{self._next_id - 1}"

        self._panes.append(pane)
        self._tab_ids.append(tab_id)
        tab = TabPane(label, pane, id=tab_id)
        # Awaited, not fire-and-forget: add_pane returns an AwaitComplete and the
        # pane's on_mount (which builds its table) has NOT run when it returns.
        # Activating or focusing before this completes reaches a half-built pane.
        await self._tabbed.add_pane(tab)
        self._tabbed.active = tab_id

        pane.focus_table()
        return pane

    async def close_active_session(self) -> bool:
        """Close the active session tab. Returns False if it's the Local tab.

        Refuses to close if there's an active transfer on this pane.
        """
        active = self.active_pane
        if active is self._local_pane:
            return False

        # Check for active transfer
        from surftp.transfer.engine import TransferEngine

        app = self.app
        if hasattr(app, "_current_engine") and app._current_engine is not None:
            # Check if the engine is using this pane's filesystem
            engine = app._current_engine
            if engine._source is active.filesystem or engine._destination is active.filesystem:
                return False  # Refuse to close

        assert self._tabbed is not None
        active_id = active.id or ""

        # Close the connection if remote
        if active.is_remote:
            await active.detach_connection()

        # Remove the tab and pane
        idx = self._panes.index(active)
        self._panes.remove(active)
        self._tab_ids.pop(idx)
        # Awaited for the same reason as add_pane: the removal is not complete
        # when the call returns, and activating "local" mid-removal races it.
        await self._tabbed.remove_pane(active_id)

        # Activate the Local tab
        self._tabbed.active = "local"
        assert self._local_pane is not None
        self._local_pane.focus_table()
        return True

    async def on_unmount(self) -> None:
        """Close every connection this side owns.

        The teardown lives here, not on the app: by the time ``App.on_unmount``
        runs its children are already gone, so an app-level sweep finds no panes
        and silently leaks every SSH connection — the server never sees a
        disconnect and the process hangs waiting on it.
        """
        for pane in self._panes:
            if pane.is_remote:
                await pane.close_connection()

    def _focus_when_ready(self, pane: FilePane) -> None:
        """Focus a pane's table now, or once it has finished mounting.

        Tab activation can fire while the pane is still being mounted (Textual
        posts ``TabActivated`` as soon as ``active`` is assigned). Focusing a
        half-built pane raised ``AssertionError`` and killed the app, so the
        attempt is retried after a refresh instead of assumed to succeed.
        """
        if pane.focus_table():
            return
        self.call_after_refresh(pane.focus_table)

    def activate(self, index: int) -> None:
        """Activate the tab at the given index (0-based, where 0 is Local)."""
        if self._tabbed is None or index < 0 or index >= len(self._panes):
            return
        pane = self._panes[index]
        tab_id = self._tab_ids[index]
        self._tabbed.active = tab_id
        self._focus_when_ready(pane)

    def next_session(self) -> None:
        """Activate the next session tab, wrapping around."""
        if self._tabbed is None or len(self._panes) <= 1:
            return
        active = self.active_pane
        idx = self._panes.index(active)
        next_idx = (idx + 1) % len(self._panes)
        self.activate(next_idx)

    def previous_session(self) -> None:
        """Activate the previous session tab, wrapping around."""
        if self._tabbed is None or len(self._panes) <= 1:
            return
        active = self.active_pane
        idx = self._panes.index(active)
        prev_idx = (idx - 1) % len(self._panes)
        self.activate(prev_idx)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Focus the table when a tab is activated, not the tab bar.

        Only when this side already holds focus. At start-up *both* sides mount
        their Local tab and fire this event; a side that grabbed focus then
        would steal it from the pane the app had just focused, leaving the app
        pointing at the right-hand pane on launch. Deliberate tab switches
        (:meth:`activate`, :meth:`open_session`) focus explicitly instead.
        """
        if not self.has_focus_within:
            return
        tab_id = event.pane.id or ""
        for i, tid in enumerate(self._tab_ids):
            if tid == tab_id:
                # Focus the table, never the tab bar
                self._focus_when_ready(self._panes[i])
                break
