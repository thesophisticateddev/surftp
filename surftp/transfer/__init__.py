"""Transfer engine: copy files and directory trees between two ``FileSystem`` objects.

The engine is protocol-agnostic: it copies between two ``FileSystem`` instances
and never learns which protocols it is bridging. Local→remote, remote→local,
and local→local all use the same code path.

Key design decisions (see ``.plans/plan-transfers.md``):

- **Concurrency window, not a thread pool.** An ``asyncio.Semaphore`` bounds
  in-flight files. The transport is I/O-bound; a thread pool would buy nothing.
- **Channels, never new connections.** Concurrent SFTP transfers open extra
  channels on the existing ``SSHSession``. The benchmark shows extra connections
  bought no throughput gain.
- **Progress is throttled.** Posting a Textual message per chunk would starve
  the UI. The engine coalesces progress updates on a 100 ms timer.
- **Cancellation cleans up.** Cancelling closes in-flight handles and deletes
  partial destination files. An unmarked half-file is worse than no file.
- **One failure does not kill the batch.** Per item: retry twice on transient
  errors, then mark ``FAILED`` and continue.
"""

from __future__ import annotations

from surftp.transfer.engine import TransferEngine
from surftp.transfer.planner import plan_transfer
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

__all__ = [
    "ConflictPolicy",
    "Progress",
    "TransferEngine",
    "TransferError",
    "TransferItem",
    "TransferJob",
    "TransferResult",
    "TransferState",
    "Tuning",
    "plan_transfer",
    "tuning_for_items",
]
