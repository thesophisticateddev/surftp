"""Transfer panel: queue table + aggregate progress bar.

The panel shows one row per item in the active transfer job, with live
progress updates. An aggregate bar at the bottom shows overall throughput
and ETA. The panel is collapsible via ``ctrl+t``.

Progress updates are throttled by the engine (100 ms flush interval), so
the panel does not need its own coalescing — it just renders whatever the
engine pushes.
"""

from __future__ import annotations

import time

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Static

from surftp.fs.types import format_size
from surftp.transfer.types import Progress, TransferState


class TransferPanel(Vertical):
    """A collapsible panel showing the transfer queue and aggregate progress."""

    DEFAULT_CSS = """
    TransferPanel {
        height: 12;
        border-top: solid $primary;
        padding: 0 1;
    }
    TransferPanel.hidden {
        display: none;
    }
    TransferPanel DataTable {
        height: 1fr;
    }
    TransferPanel #aggregate {
        height: 1;
        color: $text-muted;
    }
    """

    visible: reactive[bool] = reactive(True)

    def __init__(self, *args, **kwargs) -> None:
        """Create an empty transfer panel."""
        super().__init__(*args, **kwargs)
        self._table: DataTable[str] | None = None
        self._aggregate: Static | None = None
        self._row_keys: list[str] = []  # source paths in row order
        self._start_time: float = 0.0
        self._total_bytes: int = 0
        self._bytes_done: int = 0

    def compose(self) -> ComposeResult:
        """Yield the queue table and the aggregate bar."""
        yield DataTable[str](cursor_type="row", zebra_stripes=True, show_cursor=False)
        yield Static("", id="aggregate")

    def on_mount(self) -> None:
        """Capture the table and aggregate bar, configure columns."""
        self._table = self.query_one(DataTable)
        self._aggregate = self.query_one("#aggregate", Static)
        self._table.add_columns("Name", "Size", "Progress", "State")

    def watch_visible(self, old: bool, new: bool) -> None:
        """Toggle the ``hidden`` class when visibility changes."""
        if new:
            self.remove_class("hidden")
        else:
            self.add_class("hidden")

    def start_job(self, total_bytes: int) -> None:
        """Prepare the panel for a new job."""
        assert self._table is not None
        self._table.clear()
        self._row_keys.clear()
        self._start_time = time.monotonic()
        self._total_bytes = total_bytes
        self._bytes_done = 0
        self._update_aggregate()

    def add_item(self, source_path: str, name: str, size: int) -> None:
        """Add a row for one item."""
        assert self._table is not None
        self._table.add_row(
            name,
            format_size(size, False),
            "",
            "queued",
            key=source_path,
        )
        self._row_keys.append(source_path)

    def update_progress(self, progress: Progress) -> None:
        """Update one row's progress and state, then refresh the aggregate."""
        assert self._table is not None
        if progress.item.source_path not in self._row_keys:
            return

        try:
            row_idx = self._row_keys.index(progress.item.source_path)
        except ValueError:
            return

        if progress.item.is_directory:
            self._table.update_cell_at((row_idx, 3), _state_text(progress.state))
            if progress.state is TransferState.DONE:
                self._table.update_cell_at((row_idx, 2), "created")
            return

        if progress.state is TransferState.RUNNING:
            pct = (progress.bytes_done / progress.item.size * 100) if progress.item.size else 100
            self._table.update_cell_at((row_idx, 2), f"{pct:.0f}%")
            self._table.update_cell_at((row_idx, 3), "transferring")
        elif progress.state is TransferState.DONE:
            self._table.update_cell_at((row_idx, 2), "100%")
            self._table.update_cell_at((row_idx, 3), "done")
        elif progress.state is TransferState.FAILED:
            err = progress.error or "error"
            self._table.update_cell_at((row_idx, 2), "")
            self._table.update_cell_at((row_idx, 3), f"failed: {err[:20]}")
        elif progress.state is TransferState.SKIPPED:
            self._table.update_cell_at((row_idx, 2), "")
            self._table.update_cell_at((row_idx, 3), "skipped")
        elif progress.state is TransferState.CANCELLED:
            self._table.update_cell_at((row_idx, 2), "")
            self._table.update_cell_at((row_idx, 3), "cancelled")

        self._recalculate_bytes()
        self._update_aggregate()

    def _recalculate_bytes(self) -> None:
        """Recompute total bytes done from the table cells."""
        self._bytes_done = 0
        for i, source_path in enumerate(self._row_keys):
            try:
                state_cell = self._table.get_cell_at((i, 3)) if self._table else ""
            except (IndexError, KeyError):
                continue
            if state_cell == "done":
                try:
                    size_text = self._table.get_cell_at((i, 1)) if self._table else "0"
                except (IndexError, KeyError):
                    continue
                self._bytes_done += _parse_size(size_text)

    def _update_aggregate(self) -> None:
        """Refresh the aggregate bar with throughput and ETA."""
        assert self._aggregate is not None
        elapsed = time.monotonic() - self._start_time if self._start_time else 0
        if elapsed > 0 and self._bytes_done > 0:
            rate = self._bytes_done / elapsed
            rate_text = f"{format_size(int(rate), False)}/s"
            remaining = self._total_bytes - self._bytes_done
            eta = remaining / rate if rate > 0 else 0
            if eta < 60:
                eta_text = f"ETA {int(eta)}s"
            elif eta < 3600:
                eta_text = f"ETA {int(eta // 60)}m{int(eta % 60)}s"
            else:
                eta_text = f"ETA {int(eta // 3600)}h{int((eta % 3600) // 60)}m"
        else:
            rate_text = "--/s"
            eta_text = ""
        pct = (self._bytes_done / self._total_bytes * 100) if self._total_bytes else 0
        self._aggregate.update(
            f"{format_size(self._bytes_done, False)} / {format_size(self._total_bytes, False)} "
            f"({pct:.0f}%)  {rate_text}  {eta_text}"
        )

    def clear(self) -> None:
        """Clear the panel."""
        assert self._table is not None
        self._table.clear()
        self._row_keys.clear()
        self._bytes_done = 0
        self._total_bytes = 0
        self._update_aggregate()


def _state_text(state: TransferState) -> str:
    """Render a ``TransferState`` as a short human-readable string."""
    match state:
        case TransferState.QUEUED:
            return "queued"
        case TransferState.RUNNING:
            return "transferring"
        case TransferState.DONE:
            return "done"
        case TransferState.FAILED:
            return "failed"
        case TransferState.SKIPPED:
            return "skipped"
        case TransferState.CANCELLED:
            return "cancelled"


def _parse_size(text: str) -> int:
    """Parse a ``format_size`` output back to bytes (approximate)."""
    text = text.strip()
    if text == "<DIR>":
        return 0
    parts = text.split()
    if len(parts) != 2:
        return 0
    try:
        value = float(parts[0])
    except ValueError:
        return 0
    unit = parts[1]
    multipliers = {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
    return int(value * multipliers.get(unit, 1))
