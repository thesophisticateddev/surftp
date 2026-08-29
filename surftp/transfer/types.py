"""Data model for file transfers.

``TransferItem`` is one file (or one directory to create) in a job.
``TransferJob`` is one user action — a batch of items moving in one direction.
``Progress`` is the mutable state of one item, updated per chunk by the engine.

``Progress`` is deliberately mutable against the house style: allocating a
frozen dataclass per 128 KB chunk would be thousands of allocations per second
per file for no benefit. The mutation is confined to the engine; the UI only
reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class TransferState(StrEnum):
    """The lifecycle of one item in a transfer job."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class ConflictPolicy(StrEnum):
    """What to do when a destination file already exists.

    ``ASK`` is the default: the UI presents a modal with Overwrite / Skip /
    Rename options, with Skip focused by default (the non-destructive choice).
    """

    ASK = "ask"
    OVERWRITE = "overwrite"
    SKIP = "skip"
    RENAME = "rename"


@dataclass(frozen=True, slots=True)
class TransferItem:
    """One file (or one directory to create) in a job.

    ``is_directory`` items are created first, in depth order, before any file
    in them starts. This ensures the destination tree exists before files
    arrive.
    """

    source_path: str
    destination_path: str
    size: int
    is_directory: bool


@dataclass(slots=True)
class Progress:
    """Mutable state of one item, updated per chunk by the engine.

    The engine writes ``bytes_done`` and ``state``; the UI reads them.
    ``error`` is set when ``state`` is ``FAILED``.
    """

    item: TransferItem
    state: TransferState = TransferState.QUEUED
    bytes_done: int = 0
    error: str | None = None


@dataclass(slots=True)
class TransferJob:
    """One user action — a batch of items moving in one direction.

    ``total_bytes`` is known before the first byte moves, so the progress bar
    can show a percentage. ``source_label`` and ``destination_label`` are
    human-readable names for the UI (e.g. ``"local:/home/user"`` or
    ``"sftp:user@host:/remote"``).
    """

    items: list[TransferItem]
    total_bytes: int
    source_label: str
    destination_label: str


@dataclass(slots=True)
class TransferResult:
    """The outcome of a completed or cancelled job.

    ``completed`` counts items that finished successfully. ``failed`` counts
    items that could not be transferred after retries. ``skipped`` counts items
    skipped due to conflict policy. ``cancelled`` counts items that were in
    flight when the user cancelled.
    """

    completed: int = 0
    failed: int = 0
    skipped: int = 0
    cancelled: int = 0
    total_bytes_transferred: int = 0


class TransferError(Exception):
    """A transfer-level failure (e.g. source unreadable, destination unwritable).

    Per-item failures are recorded in ``Progress.error`` and do not raise this;
    this is for job-level failures that abort the entire batch.
    """
