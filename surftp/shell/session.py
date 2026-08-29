"""The parent side of a remote shell: SSH channel + child emulator + back-pressure window.

One :class:`ShellSession` owns:

* an asyncssh SSH channel — opened on the **existing** authenticated
  connection, a second channel next to SFTP, never a second connection (no
  re-authentication),
* a child process running ``pyte`` (the emulator — see ``emulator.py``),
* the window between them, where the back-pressure decision lives.

Back-pressure is the point. The parent reads the SSH channel only as fast as
the child consumes it. Above ``MAX_UNACKED`` unacknowledged bytes, reading the
channel stops entirely; asyncssh then lets the flow-control window close and
the server stops sending. Measured: a flooding ``cat /dev/urandom`` costs ~6%
of concurrent SFTP throughput instead of ~57%, and can never exhaust local
memory, because the channel is never drained into an unbounded buffer.

The child pipe is **blocking**, and reading it must happen on a worker thread
(``@work(thread=True)`` in the widget layer), never the event loop. Frames
arrive from the child via :meth:`next_message`, which the UI polls; each frame
is posted to the widget with ``App.call_from_thread``.

Security: the child is spawned with the ``spawn`` context, so the parent's
asyncio loop, SSH sockets and unlocked vault DEK are not duplicated into it.
The child gets terminal bytes only — never a credential, a profile or a vault
handle — and it cannot authenticate anything. The shell stream, including
anything typed at a ``sudo`` prompt, is never logged or written to disk.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import multiprocessing.connection
from typing import Any

from surftp.net.ssh import SSHSession
from surftp.shell import emulator
from surftp.shell.protocol import ACK, FEED, FRAME, QUIT, RESIZE, MessageReader, frame

# Above this many unacknowledged bytes, stop reading the SSH channel entirely.
MAX_UNACKED: int = 256 * 1024
# Channel read chunk. Small: asyncssh ``read(n)`` waits until *n* bytes are
# available, so a large value would stall the drain behind a trickle of output.
READ_CHUNK: int = 4096


class ShellSession:
    """One remote shell: an SSH channel, a child emulator, and the window between them."""

    def __init__(
        self,
        process: multiprocessing.Process,
        child_conn: multiprocessing.connection.Connection,
        channel,
    ) -> None:
        """Wrap an opened shell. Use :meth:`start` rather than calling this."""
        self._process = process
        self._child_conn = child_conn
        self._channel = channel
        self._unacked = 0
        self._reader = MessageReader()
        self._channel_task: asyncio.Task | None = None
        self._exited = False
        self._exit_status: int | None = None

    @classmethod
    async def start(
        cls,
        session: SSHSession,
        cols: int,
        rows: int,
    ) -> ShellSession:
        """Open a shell on ``session``'s connection and spawn the emulator child.

        The child is spawned with the ``spawn`` context — ``fork`` would clone
        the parent's asyncio loop, its open SSH sockets and the unlocked
        vault's DEK into a second process, which is both a correctness and a
        security problem.
        """
        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=True)
        process = ctx.Process(target=emulator.main, args=(child_conn,), daemon=True)
        process.start()
        child_conn.close()  # parent only talks on parent_conn

        channel = await session.connection.create_process(
            term_type="xterm-256color",
            term_size=(cols, rows),
            encoding=None,  # bytes streams: the shell carries raw terminal bytes
        )

        shell = cls(process, parent_conn, channel)
        shell._channel_task = asyncio.create_task(shell._drain_channel())
        await shell.resize(cols, rows)
        return shell

    # ------------------------------------------------------------------
    # Child pipe (read on a worker thread by the UI)
    # ------------------------------------------------------------------

    def next_message(self, timeout: float | None = None) -> dict | None:
        """Blocking read of the next message from the child, or ``None`` on timeout.

        Updates the back-pressure window as acks arrive. This call is meant to
        run on a worker thread — the pipe read blocks, and a blocking read on
        the event loop would defeat the whole design.
        """
        if self._exited:
            return None
        try:
            if not self._child_conn.poll(timeout):
                return None
            data = self._child_conn.recv_bytes()
        except (EOFError, OSError):
            self._exited = True
            return None
        messages = self._reader.feed(data)
        result: dict | None = None
        for message in messages:
            if message.get("t") == ACK:
                self._unacked = max(0, self._unacked - int(message.get("bytes", 0)))
            elif message.get("t") == FRAME:
                result = message  # the UI wants the latest frame
        return result

    # ------------------------------------------------------------------
    # Channel drain: SSH output -> child, honouring back-pressure
    # ------------------------------------------------------------------

    async def _drain_channel(self) -> None:
        """Read SSH stdout and forward it to the child, honouring back-pressure.

        When ``_unacked`` exceeds ``MAX_UNACKED`` the read stops entirely and
        the loop sleeps; asyncssh closes the flow-control window, the server
        stalls, and SFTP on the same connection keeps its throughput. This is
        the row that makes a runaway command harmless — a terminal that refuses
        to read faster than it can render is self-defending.
        """
        try:
            while not self._exited:
                if self._unacked >= MAX_UNACKED:
                    await asyncio.sleep(0.05)
                    continue
                try:
                    data = await self._channel.stdout.read(READ_CHUNK)
                except asyncio.CancelledError:
                    return
                except Exception:
                    return  # channel failed; the shell is effectively over
                if not data:
                    self._mark_exited()
                    return  # EOF: remote closed the shell
                self._unacked += len(data)
                self._send_to_child({"t": FEED, "d": data})
        except (asyncio.CancelledError, OSError):
            pass

    def _send_to_child(self, message: dict) -> None:
        """Write one framed message to the child's end of the pipe.

        Synchronous: the pipe is buffered by the OS, and the child's own
        back-pressure (via its stdin) is what throttles us — the same
        mechanism that throttles the server.
        """
        try:
            self._child_conn.send_bytes(frame(message))
        except (OSError, EOFError):
            self._exited = True

    def _mark_exited(self) -> None:
        """Record that the remote shell has exited, capturing its status."""
        self._exited = True
        try:
            if getattr(self._channel, "exit_status", None) is not None:
                self._exit_status = self._channel.exit_status
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send_input(self, data: bytes) -> None:
        """Send typed bytes to the remote shell."""
        if self._exited or self._channel is None:
            return
        try:
            self._channel.stdin.write(data)
            await self._channel.stdin.drain()
        except (OSError, Exception):
            pass

    async def resize(self, cols: int, rows: int) -> None:
        """Resize the remote terminal *and* the local emulator.

        Both must happen: only changing one leaves the remote's idea of the
        screen diverging from the emulator's, and full-screen apps corrupt.
        """
        if self._channel is not None and not self._exited:
            try:
                self._channel.change_terminal_size(cols, rows)
            except (OSError, Exception):
                pass
        self._send_to_child({"t": RESIZE, "cols": cols, "rows": rows})

    @property
    def exited(self) -> bool:
        """Whether the remote shell has exited."""
        return self._exited

    @property
    def exit_status(self) -> int | None:
        """The remote shell's exit status, once it has exited."""
        return self._exit_status

    async def close(self) -> None:
        """Kill the child (always) and close the SSH channel.

        The child must never be left running: it is a process that survives
        the app, unlike an SSH connection. Closing the shell leaves the
        underlying ``SSHSession`` — and SFTP on it — untouched.
        """
        self._exited = True
        if self._channel_task is not None:
            self._channel_task.cancel()
        # Tell the child to quit, then terminate unconditionally.
        try:
            self._child_conn.send_bytes(frame({"t": QUIT}))
        except (OSError, EOFError):
            pass
        if self._process is not None and self._process.is_alive():
            self._process.terminate()
            try:
                self._process.join(timeout=2)
            except Exception:
                pass
            if self._process.is_alive():
                self._process.kill()
        try:
            self._child_conn.close()
        except OSError:
            pass
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception:
                pass