"""The tabbed bottom panel: Transfers | Shell: <session> | …

Replaces the bare ``TransferPanel`` with a ``TabbedContent`` whose first tab is
the (unchanged) transfer monitor and whose remaining tabs are one per open
shell. The transfers tab keeps its own ``ctrl+t`` toggle; the shell tabs are
added/removed as shells open and close.

The panel owns no emulator state — each shell tab hosts a ``TerminalView``
that displays frames from the ``ShellSession`` in ``surftp.shell``.

**Do not override ``compose()``** to declare tabs directly. The base
``TabbedContent.compose()`` is what builds the internal ``ContentTabs`` widget
that ``add_pane``/``remove_pane`` rely on; every tab here — including the
Transfers tab — is added via :meth:`add_pane`, so that widget exists exactly
once and all tabs are uniform.
"""

from __future__ import annotations

from textual.reactive import reactive
from textual.widgets import TabbedContent, TabPane

from surftp.shell.session import ShellSession
from surftp.widgets.terminal import TerminalView
from surftp.widgets.transfers import TransferPanel

# Labels longer than this are truncated so the tab bar does not scroll.
MAX_LABEL: int = 18


class BottomPanel(TabbedContent):
    """Tabbed container: transfers monitor plus one tab per shell."""

    visible: reactive[bool] = reactive(True)

    def __init__(self, *args, **kwargs) -> None:
        """Create the bottom panel with a transfers tab."""
        super().__init__(*args, **kwargs)
        self._transfers: TransferPanel | None = None
        self._shell_views: dict[str, TerminalView] = {}  # shell tab id -> view

    def on_mount(self) -> None:
        """Add the transfers tab and make the tab bar non-focusable."""
        transfers = TransferPanel(id="transfer-content")
        self.add_pane(TabPane("Transfers", transfers, id="transfers"))
        self.active = "transfers"
        # The TransferPanel's own on_mount has not run yet, so its table does
        # not exist; capture the reference after a refresh instead.
        self.call_after_refresh(self._grab_transfers)
        # The Tabs widget appears as panes are added; make it non-focusable so
        # shell keys reach the terminal. Retry after refresh if not up yet.
        self.call_after_refresh(self._disable_tab_focus)

    def _disable_tab_focus(self) -> None:
        """Make the underlying Tabs bar non-focusable."""
        try:
            tabs = self.query_one("Tabs")
        except Exception:
            self.call_after_refresh(self._disable_tab_focus)
            return
        tabs.can_focus = False

    def _grab_transfers(self) -> None:
        """Capture the transfers panel reference for the app."""
        try:
            self._transfers = self.query_one("#transfer-content", TransferPanel)
        except Exception:
            self._transfers = None

    @property
    def transfers(self) -> TransferPanel:
        """The transfers monitor tab."""
        assert self._transfers is not None, "BottomPanel not mounted yet"
        return self._transfers

    def _on_tab_pane_focused(self, event: TabPane.Focused) -> None:
        """Ignore ``TabPane.Focused`` events entirely.

        The base ``TabbedContent`` handler sets ``self.active`` when a
        descendant pane is focused — but it assumes a ``ContentTabs`` child
        exists, which only happens when panes are added via ``add_pane``.
        ``BottomPanel`` declares its Transfers pane in ``compose()``, so that
        child never exists and the handler raises ``NoMatches``.

        It would also be wrong to react here: the session tabs live in sibling
        ``TabbedContent`` widgets, and their panes' focus events bubble to us
        too. Activating a tab we do not own (e.g. the sessions' ``local`` tab)
        would raise. The panel's tabs are activated explicitly by
        :meth:`add_shell` / :meth:`remove_shell`, never by focus.
        """
        event.stop()

    def watch_visible(self, old: bool, new: bool) -> None:
        """Toggle visibility of the whole panel."""
        self.styles.display = "block" if new else "none"

    # ------------------------------------------------------------------
    # Shell tabs
    # ------------------------------------------------------------------

    def add_shell(self, session: ShellSession, label: str) -> str:
        """Add a shell tab and return its id.

        A ``TerminalView`` is created for the session and the tab is
        activated so the user is dropped straight into the shell.
        """
        view = TerminalView(id="shell-view")
        view.shell = session
        label = label[:MAX_LABEL]
        tab_id = f"shell-{len(self._shell_views)}"
        tab = TabPane(label, view, id=tab_id)
        self._shell_views[tab_id] = view
        self.add_pane(tab)
        self.active = tab_id
        self.call_after_refresh(view.focus)
        return tab_id

    def view_for(self, tab_id: str) -> TerminalView | None:
        """The TerminalView backing a shell tab, or ``None``."""
        return self._shell_views.get(tab_id)

    def remove_shell(self, tab_id: str) -> None:
        """Remove a shell tab by id, if present."""
        view = self._shell_views.pop(tab_id, None)
        if view is None:
            return
        try:
            self.remove_pane(tab_id)
        except Exception:
            pass
        if not self._shell_views:
            self.active = "transfers"

    @property
    def has_shells(self) -> bool:
        """Whether any shell tab is open."""
        return bool(self._shell_views)