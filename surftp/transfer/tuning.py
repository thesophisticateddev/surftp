"""Per-protocol tuning: block size, pipeline depth, concurrency.

The plan's benchmark showed that a single Python SFTP stream saturates ~3 Gbit/s
on loopback, so the transport is not CPU-bound at any realistic network speed.
The win is concurrency — not cores — and it is largest exactly where users hurt:
small files are latency-bound.

The engine adapts by median item size:

- **Small files (< 1 MB):** concurrency 16, pipeline 32, block 128 KB. Many
  files in flight, shallow pipeline per file.
- **Large files (>= 1 MB):** concurrency 4, pipeline 128, block 256 KB. Fewer
  files in flight, deeper pipeline per file.

These are starting points to be re-measured, not truths. The benchmark script
in ``.plans/plan-transfers.md`` §0 is the source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass

from surftp.transfer.types import TransferItem

SMALL_FILE_THRESHOLD: int = 1_048_576  # 1 MB


@dataclass(frozen=True, slots=True)
class Tuning:
    """Tuning parameters for the transfer engine.

    ``concurrency`` bounds in-flight files via an ``asyncio.Semaphore``.
    ``pipeline_depth`` is the SFTP read/write window (``max_requests``).
    ``block_size`` is the chunk size for reads and writes.
    """

    concurrency: int
    pipeline_depth: int
    block_size: int


def tuning_for_items(items: list[TransferItem]) -> Tuning:
    """Pick tuning parameters based on the median item size.

    Directories (``is_directory=True``) are excluded from the median calculation
    because they have ``size=0`` and do not transfer bytes. If all items are
    directories, the small-file tuning is used (directories are fast).
    """
    file_sizes = sorted(item.size for item in items if not item.is_directory)
    if not file_sizes:
        return _small_file_tuning()
    median = file_sizes[len(file_sizes) // 2]
    if median < SMALL_FILE_THRESHOLD:
        return _small_file_tuning()
    return _large_file_tuning()


def _small_file_tuning() -> Tuning:
    """Tuning for many small files: high concurrency, shallow pipeline."""
    return Tuning(concurrency=16, pipeline_depth=32, block_size=131_072)


def _large_file_tuning() -> Tuning:
    """Tuning for few large files: low concurrency, deep pipeline."""
    return Tuning(concurrency=4, pipeline_depth=128, block_size=262_144)
