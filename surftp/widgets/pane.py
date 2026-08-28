"""One directory-listing pane.

``FilePane`` is deliberately protocol-blind: it renders whatever ``FileEntry``
rows its ``FileSystem`` backend returns and never imports ``os`` or
``pathlib``. Connecting a pane to a remote host is swapping the backend object
— nothing about the rendering, the cursor, or the key handling changes.

Listing runs on an **exclusive worker**. A remote ``readdir`` can take seconds
or hang on a dead connection, and doing it inline would freeze the whole app
including the other pane. ``exclusive=True`` also cancels a superseded listing
when the user navigates faster than the server answers, which is what keeps
rapid ``enter`` presses from painting directories out of order.
"""

from __future__ import annotations

from textual import work
from textual.reactive import reactive
from textual.widgets import DataTable, Static

from surftp.fs import (
    FileEntry,
    FileSystem,
    FileSystemError,
    LocalFileSystem,
    format_modified,
    format_size,
)
from surftp.net.connect import RemoteConnection


class FilePane(Static):
    """A single file pane: a border-titled container wrapping a row-cursor ``DataTable``."""

    DEFAULT_CSS = """
    FilePane {
        height: 1fr;
        padding: 0 1;
    }
    FilePane DataTable {
        height: 1fr;
    }
    """

    path: reactive[str] = reactive("", init=False)
    show_hidden: reactive[bool] = reactive(False, init=False)

    def __init__(
        self,
        filesystem: FileSystem,
        path: str,
        *,
        id: str | None = None,
    ) -> None:
        """Create a pane backed by ``filesystem`` rooted at ``path``."""
        super().__init__(id=id)
        self._filesystem = filesystem
        self._path = path
        self._table: DataTable[str] | None = None
        self._entries: list[FileEntry] = []
        self._connection: RemoteConnection | None = None
        # Where to return to when a remote connection is dropped, so
        # disconnecting lands the user where they left off locally.
        self._local_path = path

    def compose(self):
        """Yield the pane's single child: the listing table."""
        yield DataTable[str](cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        """Capture the table, configure its columns, and perform the first load."""
        self._table = self.query_one(DataTable)
        self._table.add_columns("Name", "Size", "Modified", "Mode")
        self.path = self._path  # triggers watch_path -> initial load

    @property
    def table(self) -> DataTable[str]:
        """The listing table; safe to call only after mount."""
        assert self._table is not None
        return self._table

    @property
    def filesystem(self) -> FileSystem:
        """The backend currently driving this pane."""
        return self._filesystem

    @property
    def is_remote(self) -> bool:
        """Whether this pane is currently showing a remote host."""
        return self._connection is not None

    @property
    def connection(self) -> RemoteConnection | None:
        """The live remote connection, or ``None`` for a local pane."""
        return self._connection

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    @work(exclusive=True)
    async def load_directory(self, restore_cursor: int | None = None) -> None:
        """Repopulate the table from the backend, off the event loop.

        ``FileSystemError`` is caught and shown in the border subtitle — an
        unreadable directory, a dropped connection or a permission failure must
        never kill the app. The table is only cleared once the new listing has
        arrived, so a failed load leaves the previous contents on screen instead
        of blanking the pane.
        """
        self._set_title()
        self.border_subtitle = "Loading…"
        try:
            entries = await self._filesystem.list_directory(self.path)
        except FileSystemError as exc:
            self.border_subtitle = f"Error: {exc}"
            return

        if not self.show_hidden:
            entries = [e for e in entries if e.name == ".." or not e.name.startswith(".")]

        table = self.table
        table.clear()
        self._entries = entries
        for entry in entries:
            table.add_row(
                entry.name,
                format_size(entry.size, entry.is_dir),
                format_modified(entry.modified),
                "d" if entry.is_dir else "-",
            )
        self.border_subtitle = f"{len(entries)} items"
        if restore_cursor is not None and table.row_count:
            table.move_cursor(row=min(restore_cursor, table.row_count - 1))

    def _set_title(self) -> None:
        """Render the border title as ``label:path`` for remote, bare path for local."""
        label = self._filesystem.label
        self.border_title = f"{label}:{self.path}" if self.is_remote else self.path

    def watch_path(self, old: str, new: str) -> None:
        """Reactive hook: reload the listing whenever the path changes."""
        if not self.is_remote:
            self._local_path = new
        self.load_directory()

    def watch_show_hidden(self, old: bool, new: bool) -> None:
        """Reactive hook: reload the listing when hidden-file visibility flips."""
        self.load_directory()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def selected_entry(self) -> FileEntry | None:
        """Return the entry under the cursor, or ``None`` on an empty pane."""
        if not self._entries:
            return None
        row = self.table.cursor_row
        if row < 0 or row >= len(self._entries):
            return None
        return self._entries[row]

    def open_selected(self) -> None:
        """Descend into the selected directory (or follow ``..``); no-op on files."""
        entry = self.selected_entry()
        if entry is None or not entry.is_dir:
            return
        self.path = entry.path

    def go_parent(self) -> None:
        """Move this pane to its parent directory (no-op at the root).

        Reads the parent from the backend rather than computing it locally: a
        remote POSIX parent is not the same string operation as a local one on
        every platform.
        """
        self._go_parent()

    @work(exclusive=False)
    async def _go_parent(self) -> None:
        """Await the backend's parent path, then navigate. See :meth:`go_parent`."""
        parent = await self._filesystem.parent_of(self.path)
        if parent != self.path:
            self.path = parent

    def refresh_listing(self) -> None:
        """Re-read the current path, preserving the cursor row where possible."""
        self.load_directory(restore_cursor=self.table.cursor_row)

    # ------------------------------------------------------------------
    # Connection state
    # ------------------------------------------------------------------

    def attach_connection(self, connection: RemoteConnection) -> None:
        """Point this pane at a live remote connection and list its initial directory."""
        self._connection = connection
        self._filesystem = connection.filesystem
        self.path = connection.initial_path

    async def detach_connection(self) -> None:
        """Close any remote connection and return the pane to the local disk.

        Restores the last local path rather than the current remote one, which
        would be meaningless (and possibly nonexistent) locally.
        """
        connection, self._connection = self._connection, None
        self._filesystem = LocalFileSystem()
        if connection is not None:
            await connection.close()
        self.path = self._local_path
        self.load_directory()  # in case the local path was already current
