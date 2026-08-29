# Plan: File Transfers Between Panes

**Goal:** copy files and directory trees between the two panes in either direction — local→remote,
remote→local, and local→local — with a visible queue, live progress, cancellation, and per-file
failures that do not abort the batch.

**In scope:** the transfer engine, the `FileSystem` extensions it needs, the queue/progress UI, F5/F6
bindings, conflict resolution, and verification. **Out of scope:** the SSH tunnel
(`plan-tunnel.md`), remote↔remote (pane-to-pane between two *different* servers — it needs a relay
through local storage and deserves its own plan), and directory synchronisation/diff.

---

## 0. The performance question, settled with measurements

This plan was preceded by a question about multi-core processing and a possible Rust core. Both were
tested on this machine (12 cores, loopback, asyncssh 2.24, Python 3.12) before writing any design.
The benchmarks live in the session record; the numbers that drive this design:

| Measurement | Result |
| --- | --- |
| Local disk copy (page-cache warm) | ~5160 MB/s |
| AES-256-GCM, one core, via `cryptography`/OpenSSL | ~5875 MB/s |
| **SFTP, 1 stream, `chacha20-poly1305` (asyncssh default here)** | **~237 MB/s** |
| **SFTP, 1 stream, `aes256-gcm@openssh.com`** | **~367–392 MB/s** |
| SFTP, 1 stream, `aes128-ctr` | ~294 MB/s |
| 2–4 concurrent connections, one process | ~385–391 MB/s aggregate (no gain) |
| 2–8 connections spread over separate OS processes | ~165–203 MB/s aggregate (**worse**) |
| 500 × 8 KB files, sequential | 469 files/s |
| 500 × 8 KB files, 8 concurrent | 1465 files/s |
| 500 × 8 KB files, 64 concurrent | **1661 files/s (3.5× sequential)** |

Four conclusions, and every design decision below follows from them:

1. **A single Python SFTP stream already saturates ~3 Gbit/s.** A gigabit link tops out at 125 MB/s —
   a third of what one stream does. The transport is not CPU-bound at any realistic network speed.
2. **Choosing the cipher is worth +65% for one line of code.** `aes256-gcm` beats the negotiated
   `chacha20-poly1305` by that much on any machine with AES-NI, which is every x86-64 CPU since 2010.
   No amount of language choice competes with picking the right cipher.
3. **Multiple OS processes made throughput *worse*, not better.** Process spawn plus re-importing
   asyncssh costs more than it returns, and the work is I/O-bound anyway. Multi-core is the wrong
   axis: `multiprocessing` in the transfer path would be a measurable regression.
4. **Concurrency — not cores — is the whole win, and it is largest exactly where users hurt.** Small
   files are latency-bound: 3.5× on loopback, where round-trip time is ~0.05 ms. On a real 30 ms link
   the sequential case collapses to roughly 15 files/s while a concurrent one stays near the pipeline
   depth, which is a 30–60× difference. **The engine is therefore built around a concurrency window,
   with no process pool anywhere in the network path.**

*Caveat, recorded honestly:* the benchmark server was a single Python process, so the multi-connection
rows are confounded by server-side GIL contention. Real servers are OpenSSH (C, one process per
connection). This makes the parallel numbers pessimistic — but it does not change the conclusion,
because the single-stream ceiling already exceeds any link SURFTP will meet, and that number is
uncontaminated.

### Where multiple cores *are* allowed

Exactly one place: **local checksum computation** for post-transfer verification, and only over a
threshold (say 64 MB of data) where the hash is a measurable share of wall time. `hashlib` releases
the GIL, so a `ThreadPoolExecutor` — not processes — is sufficient. Section 6 makes this opt-in and
off by default, because a checksum doubles local disk reads to defend against a failure mode SFTP's
own integrity checking already covers.

---

## 1. `FileSystem` extensions

Transfers need four members beyond today's `list_directory` / `parent_of` / `is_directory` / `label`.
Adding them to the protocol keeps the seam intact: the engine copies between two `FileSystem`
objects and never learns which protocols it is bridging.

```python
async def open_read(self, path: str) -> AsyncFileReader: ...
async def open_write(self, path: str, size_hint: int = 0) -> AsyncFileWriter: ...
async def make_directory(self, path: str) -> None: ...
async def stat(self, path: str) -> FileEntry: ...
```

`AsyncFileReader` / `AsyncFileWriter` are `Protocol`s with `read(n) -> bytes`, `write(data) -> None`
and `close()`. Three implementations:

* **`LocalFileSystem`** — `open()` behind `asyncio.to_thread`. Disk reads are fast but not instant,
  and a 4 GB read on the event loop would freeze the UI exactly as a hung socket would.
* **`SFTPFileSystem`** — `SFTPClient.open()`, whose file objects already pipeline reads/writes with
  a configurable window (`block_size`, `max_requests`).
* **`FTPFileSystem`** — `aioftp`'s `download_stream` / `upload_stream`.

**`SCPTransfer` stays a special case.** `asyncssh.scp` copies whole files and reports no progress; it
is retained for the `Protocol.SCP` profile but drives a coarse per-file progress bar rather than the
byte-level one. Documented in the UI, not hidden.

---

## 2. Module layout

```
surftp/transfer/
  __init__.py
  types.py      # TransferItem, TransferJob, TransferState, Progress, ConflictPolicy, TransferError
  planner.py    # walk a source tree -> flat list of TransferItem, with sizes and mkdir order
  engine.py     # TransferEngine: the concurrency window, retries, cancellation
  tuning.py     # per-protocol block size / pipeline depth / concurrency, and the cipher preference
surftp/widgets/
  transfers.py  # TransferPanel: queue table + aggregate progress bar
```

`planner.py` is separate from `engine.py` for one reason worth stating: **the total byte count must
be known before the first byte moves**, or the progress bar cannot show a percentage and the user
cannot tell a stalled transfer from a slow one. Walking is itself I/O and must be cancellable.

---

## 3. Data model (`types.py`)

```python
class TransferState(StrEnum):
    QUEUED = "queued"; RUNNING = "running"; DONE = "done"
    FAILED = "failed"; SKIPPED = "skipped"; CANCELLED = "cancelled"

class ConflictPolicy(StrEnum):
    ASK = "ask"; OVERWRITE = "overwrite"; SKIP = "skip"; RENAME = "rename"

@dataclass(frozen=True, slots=True)
class TransferItem:
    """One file (or one directory to create) in a job."""
    source_path: str
    destination_path: str
    size: int
    is_directory: bool

@dataclass(slots=True)
class Progress:
    """Mutable, deliberately: it is updated per chunk and read by the UI."""
    item: TransferItem
    state: TransferState
    bytes_done: int = 0
    error: str | None = None

@dataclass(slots=True)
class TransferJob:
    """One user action — a batch of items moving in one direction."""
    items: list[TransferItem]
    total_bytes: int
    source_label: str
    destination_label: str
```

`Progress` is the one mutable model in the codebase, against the house style. The alternative —
allocating a frozen dataclass per 128 KB chunk — is thousands of allocations per second per file for
no benefit. The mutation is confined to the engine; the UI only reads.

---

## 4. The engine (`engine.py`)

```python
class TransferEngine:
    """Copies a TransferJob between two FileSystems, N files at a time."""

    def __init__(self, source: FileSystem, destination: FileSystem,
                 tuning: Tuning, on_progress: Callable[[Progress], None]) -> None: ...

    async def run(self, job: TransferJob) -> TransferResult: ...
    def cancel(self) -> None: ...
```

Behaviour that is easy to get wrong, in the order it bites:

* **Concurrency window, not a thread pool.** An `asyncio.Semaphore(tuning.concurrency)` bounds
  in-flight files. Directories are created first, in depth order, before any file in them starts.
* **Adaptive concurrency.** One big file wants a *deep pipeline on one channel*; a thousand small
  files want *many files in flight*. `tuning.py` picks by median item size: `< 1 MB` → concurrency 16,
  pipeline 32; `>= 1 MB` → concurrency 4, pipeline 128 with 256 KB blocks. These are starting points
  to be re-measured, not truths.
* **Channels, never new connections.** Concurrent SFTP transfers open extra channels on the existing
  `SSHSession`. This is the "one connection, many channels" rule, and the benchmark backs it: extra
  connections bought nothing.
* **Progress must be throttled.** Posting a Textual message per 128 KB chunk is ~3000 messages/second
  per file and will starve the UI — ironically making the app *feel* slower the faster the transfer
  goes. The engine coalesces: post on a 100 ms timer, not per chunk.
* **Cancellation is cooperative and must clean up.** Cancelling closes in-flight handles and
  **deletes partial destination files** (an unmarked half-file is worse than no file). Already-copied
  files stay.
* **One failure does not kill the batch.** Per item: retry twice on a transient error (`SFTPFailure`,
  timeout), then mark `FAILED` with the reason and continue. The result names what did not transfer.
* **Preserve mtime and mode where the destination supports it**, best-effort; a failure to set them
  is not a transfer failure.

---

## 5. Conflicts and overwrite safety

Before writing, `stat` the destination. On collision, apply `ConflictPolicy`; the default is `ASK`,
with a modal offering Overwrite / Overwrite all / Skip / Skip all / Rename, and **Skip focused by
default** — the same reasoning as the host-key dialog: the non-destructive option is what an unread
Enter must choose.

**Write to a temporary name and rename on completion** (`.surftp-partial-<name>`). An interrupted
transfer must never leave a truncated file sitting at the real path looking complete. Rename is atomic
on both POSIX local and SFTP.

---

## 6. Verification (opt-in)

SSH already integrity-checks every packet, and SFTP writes are acknowledged, so a checksum defends
mainly against local disk corruption and a truncated write. Therefore: **off by default**, enabled per
job. When on, hash both sides with BLAKE2b (faster than SHA-256, in `hashlib`), computing the local
side in a `ThreadPoolExecutor` — `hashlib` releases the GIL, so this is the one place threads help.
The remote side needs a server-side hash, which is an SFTP extension (`check-file`) many servers lack;
where absent, verification degrades to a size comparison and **says so** rather than implying more.

---

## 7. UI

* `F5` copy to the other pane, `F6` move (copy then delete source — and delete only after a verified
  successful copy). Both were reserved in `bindings.py` from the first pass.
* `TransferPanel` — a collapsible panel below the panes: one row per item (name, size, progress,
  state) plus an aggregate bar with throughput and ETA. `ctrl+t` toggles it.
* `escape` cancels the running job (with confirmation when more than one file remains).
* On completion the destination pane refreshes automatically; the source pane refreshes after a move.
* Errors follow the existing convention: the pane's border subtitle plus a notification, never a crash.

---

## 8. Verification of this plan

**Unit:** planner tree-walk produces correct relative destination paths and directory ordering;
conflict policies each behave; cancellation deletes partials and leaves completed files.

**Integration (real servers, as in the existing suites):** upload and download a 256 MB file and
verify byte-for-byte equality; transfer a 500-file tree and confirm the concurrent path beats the
sequential one; kill the connection mid-transfer and confirm a clean `FAILED` state with the partial
removed; transfer into a read-only remote directory and confirm one failed item does not abort the
rest; round-trip a tree local→remote→local and diff it.

**UI (pilot):** F5 with a file selected queues and completes it; the panel shows progress; the other
pane still navigates *during* a transfer (proving the event loop is free); escape cancels.

**Benchmark, kept as a script:** re-run the measurements in §0 against the tuned engine, so a future
change that halves throughput is caught as a number rather than a feeling.

---

## 9. When Rust would earn its place

Not now, and the bar is specific rather than vague. Revisit only if a measurement shows one of:

1. Sustained transfers above **~3 Gbit/s** on real hardware, where the single-stream Python ceiling
   measured in §0 actually binds — i.e. 10 GbE or faster, with a server that can keep up.
2. A local-side operation that is genuinely CPU-bound and cannot be delegated to a C library:
   whole-tree hashing for sync/diff, or block-level delta compression (an rsync algorithm).
3. Verified per-chunk interpreter overhead dominating a profile — not assumed, profiled.

If that day comes, the right shape is a **narrow PyO3 extension behind the existing `FileSystem`
protocol** — one leaf function, replaceable, with the Python implementation kept as the fallback —
not a rewrite of the transport. Rewriting the SSH layer in Rust would mean trading `asyncssh` and
OpenSSL for a far less battle-tested stack, which trades a hypothetical performance win for a real
security regression. The seam this codebase already has is what makes that decision reversible later,
which is precisely why it should not be made now.
