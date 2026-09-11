"""The transfer engine: copies a ``TransferJob`` between two ``FileSystem`` objects.

Behaviour that is easy to get wrong, in the order it bites:

- **Concurrency window, not a thread pool.** An ``asyncio.Semaphore`` bounds
  in-flight files. Directories are created first, in depth order, before any
  file in them starts.
- **Channels, never new connections.** Concurrent SFTP transfers open extra
  channels on the existing ``SSHSession``. The benchmark backs it: extra
  connections bought nothing.
- **Progress is throttled.** Posting a Textual message per chunk would starve
  the UI. The engine coalesces: ``on_progress`` is called on a 100 ms timer,
  not per chunk.
- **Cancellation is cooperative and cleans up.** Cancelling closes in-flight
  handles and deletes partial destination files. Already-copied files stay.
- **One failure does not kill the batch.** Per item: retry twice on a transient
  error, then mark ``FAILED`` with the reason and continue.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from surftp.fs.types import FileSystem, FileSystemError
from surftp.transfer.tuning import Tuning, tuning_for_items
from surftp.transfer.types import (
    ConflictPolicy,
    Progress,
    TransferError,
    TransferItem,
    TransferJob,
    TransferResult,
    TransferState,
)

if TYPE_CHECKING:
    pass

# How many times to retry a transient failure before marking an item FAILED.
MAX_RETRIES: int = 2

# How often to flush progress updates to the UI (milliseconds).
PROGRESS_FLUSH_MS: int = 100

# Prefix for partial files — an interrupted transfer must never leave a
# truncated file sitting at the real path looking complete.
PARTIAL_PREFIX: str = ".surftp-partial-"


class TransferEngine:
    """Copies a ``TransferJob`` between two ``FileSystem`` objects, N files at a time.

    The engine is protocol-agnostic: it copies between two ``FileSystem``
    instances and never learns which protocols it is bridging.
    """

    def __init__(
        self,
        source: FileSystem,
        destination: FileSystem,
        on_progress: Callable[[Progress], None],
        conflict_policy: ConflictPolicy = ConflictPolicy.ASK,
        conflict_resolver: Callable[[TransferItem], Awaitable[ConflictPolicy]] | None = None,
    ) -> None:
        """Create an engine.

        ``on_progress`` is called with each item's ``Progress`` when it changes.
        The engine throttles these calls to ``PROGRESS_FLUSH_MS`` intervals.

        ``conflict_resolver`` is called when a destination file already exists
        and ``conflict_policy`` is ``ASK``. It must return a ``ConflictPolicy``
        (``OVERWRITE``, ``SKIP`` or ``RENAME``). If ``None``, ``ASK`` falls back
        to ``SKIP``.
        """
        self._source = source
        self._destination = destination
        self._on_progress = on_progress
        self._conflict_policy = conflict_policy
        self._conflict_resolver = conflict_resolver
        self._cancelled = False
        self._progress_queue: asyncio.Queue[Progress] = asyncio.Queue()
        self._flush_task: asyncio.Task[None] | None = None

    async def run(self, job: TransferJob) -> TransferResult:
        """Run the job and return the result.

        Directories are created first (in the order the planner produced them),
        then files are copied with the concurrency window from ``tuning``.
        """
        self._cancelled = False
        tuning = tuning_for_items(job.items)
        progress_map: dict[str, Progress] = {}
        for item in job.items:
            progress_map[item.source_path] = Progress(item=item)

        self._flush_task = asyncio.create_task(self._flush_loop(progress_map))
        semaphore = asyncio.Semaphore(tuning.concurrency)

        try:
            dirs = [item for item in job.items if item.is_directory]
            files = [item for item in job.items if not item.is_directory]

            for item in dirs:
                if self._cancelled:
                    progress_map[item.source_path].state = TransferState.CANCELLED
                    continue
                await self._create_directory(item, progress_map[item.source_path])

            tasks = [
                asyncio.create_task(self._copy_file(item, progress_map[item.source_path], semaphore, tuning))
                for item in files
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._cancelled = True  # stop the flush loop
            if self._flush_task is not None:
                self._flush_task.cancel()
                try:
                    await self._flush_task
                except asyncio.CancelledError:
                    pass
            await self._flush_remaining(progress_map)

        result = TransferResult()
        for progress in progress_map.values():
            match progress.state:
                case TransferState.DONE:
                    result.completed += 1
                    result.total_bytes_transferred += progress.item.size
                case TransferState.FAILED:
                    result.failed += 1
                case TransferState.SKIPPED:
                    result.skipped += 1
                case TransferState.CANCELLED:
                    result.cancelled += 1
        return result

    def cancel(self) -> None:
        """Request cancellation. In-flight files finish; queued files are skipped."""
        self._cancelled = True

    async def _create_directory(self, item: TransferItem, progress: Progress) -> None:
        """Create one directory at the destination."""
        progress.state = TransferState.RUNNING
        self._on_progress(progress)
        try:
            await self._destination.make_directory(item.destination_path)
            progress.state = TransferState.DONE
        except FileSystemError as exc:
            progress.state = TransferState.FAILED
            progress.error = str(exc)
        self._on_progress(progress)

    async def _copy_file(
        self,
        item: TransferItem,
        progress: Progress,
        semaphore: asyncio.Semaphore,
        tuning: Tuning,
    ) -> None:
        """Copy one file with retries and partial cleanup."""
        async with semaphore:
            if self._cancelled:
                progress.state = TransferState.CANCELLED
                self._on_progress(progress)
                return

            conflict = await self._resolve_conflict(item)
            if conflict is ConflictPolicy.SKIP:
                progress.state = TransferState.SKIPPED
                self._on_progress(progress)
                return

            dest_path = item.destination_path
            if conflict is ConflictPolicy.RENAME:
                dest_path = await self._rename_destination(dest_path)

            partial_path = self._partial_path(dest_path)
            progress.state = TransferState.RUNNING
            self._on_progress(progress)

            for attempt in range(MAX_RETRIES + 1):
                if self._cancelled:
                    progress.state = TransferState.CANCELLED
                    self._on_progress(progress)
                    await self._cleanup_partial(partial_path)
                    return
                try:
                    await self._copy_once(item, partial_path, progress, tuning)
                    await self._destination.rename(partial_path, dest_path)
                    progress.state = TransferState.DONE
                    progress.bytes_done = item.size
                    self._on_progress(progress)
                    return
                except asyncio.CancelledError:
                    # Cancellation is a decision, not a transient fault: never
                    # retry it, and let it propagate so the gather unwinds.
                    progress.state = TransferState.CANCELLED
                    self._on_progress(progress)
                    await self._cleanup_partial(partial_path)
                    raise
                except Exception as exc:
                    # Deliberately broad, and only at this boundary. Each item
                    # runs as its own task under `gather(return_exceptions=True)`,
                    # so anything not caught here is swallowed and the item sits
                    # at RUNNING 0% forever with nothing shown to the user —
                    # which is precisely how a bytes/str mode bug in a backend
                    # presented as "the transfer is stuck".
                    if attempt < MAX_RETRIES and isinstance(exc, (FileSystemError, OSError)):
                        await asyncio.sleep(0.1 * (attempt + 1))
                        progress.bytes_done = 0
                        continue
                    progress.state = TransferState.FAILED
                    progress.error = f"{type(exc).__name__}: {exc}"
                    self._on_progress(progress)
                    await self._cleanup_partial(partial_path)
                    return

    async def _copy_once(
        self,
        item: TransferItem,
        partial_path: str,
        progress: Progress,
        tuning: Tuning,
    ) -> None:
        """One attempt at copying a file, writing to the partial path."""
        reader = await self._source.open_read(item.source_path)
        writer = await self._destination.open_write(partial_path, item.size)
        try:
            while True:
                chunk = await reader.read(tuning.block_size)
                if not chunk:
                    break
                await writer.write(chunk)
                progress.bytes_done += len(chunk)
                self._on_progress(progress)
        finally:
            await reader.close()
            await writer.close()

    async def _resolve_conflict(self, item: TransferItem) -> ConflictPolicy:
        """Check if the destination exists and resolve per the conflict policy."""
        try:
            await self._destination.stat(item.destination_path)
        except FileSystemError:
            return ConflictPolicy.OVERWRITE  # does not exist, no conflict

        if self._conflict_policy is ConflictPolicy.OVERWRITE:
            return ConflictPolicy.OVERWRITE
        if self._conflict_policy is ConflictPolicy.SKIP:
            return ConflictPolicy.SKIP
        if self._conflict_policy is ConflictPolicy.RENAME:
            return ConflictPolicy.RENAME
        if self._conflict_resolver is not None:
            return await self._conflict_resolver(item)
        return ConflictPolicy.SKIP  # ASK with no resolver → safe default

    async def _rename_destination(self, path: str) -> str:
        """Generate a non-colliding name by appending ``.1``, ``.2``, etc."""
        base, ext = _split_extension(path)
        for i in range(1, 1000):
            candidate = f"{base}.{i}{ext}"
            try:
                await self._destination.stat(candidate)
            except FileSystemError:
                return candidate
        return f"{base}.999{ext}"

    def _partial_path(self, dest_path: str) -> str:
        """Return the partial-file path for ``dest_path``."""
        import posixpath

        directory = posixpath.dirname(dest_path)
        name = posixpath.basename(dest_path)
        if directory:
            return posixpath.join(directory, f"{PARTIAL_PREFIX}{name}")
        return f"{PARTIAL_PREFIX}{name}"

    async def _cleanup_partial(self, partial_path: str) -> None:
        """Delete a partial file if it exists."""
        try:
            await self._destination.remove(partial_path)
        except FileSystemError:
            pass  # already gone or never created

    async def _flush_loop(self, progress_map: dict[str, Progress]) -> None:
        """Periodically call ``on_progress`` for all running items."""
        while not self._cancelled:
            await asyncio.sleep(PROGRESS_FLUSH_MS / 1000)
            for progress in progress_map.values():
                if progress.state is TransferState.RUNNING:
                    self._on_progress(progress)

    async def _flush_remaining(self, progress_map: dict[str, Progress]) -> None:
        """Final flush of all progress states."""
        for progress in progress_map.values():
            self._on_progress(progress)


def _split_extension(path: str) -> tuple[str, str]:
    """Split ``path`` into ``(base, ext)`` where ``ext`` includes the dot."""
    import posixpath

    name = posixpath.basename(path)
    directory = posixpath.dirname(path)
    dot = name.rfind(".")
    if dot <= 0:
        return path, ""
    base = posixpath.join(directory, name[:dot]) if directory else name[:dot]
    ext = name[dot:]
    return base, ext
