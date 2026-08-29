"""The terminal emulator child process: pyte + frame coalescing.

This module runs as a separate process and imports neither ``asyncssh`` nor
``textual`` — keeping it to ``pyte`` + ``msgpack`` keeps start-up near 100 ms
rather than near a second, because every import it names is paid again per
shell.

Why a process at all: the plan's measurements showed terminal emulation is the
real bottleneck (~1.2 MB/s, ~300× slower than SFTP). It is pure Python, so a
thread would not help — the GIL makes a process the only real isolation. The
SSH channel itself stays in the parent on the already-authenticated
connection; only the emulator — the part that would freeze the UI — is
isolated here.

The child *coalesces*: it drains its whole inbox, feeds it all to pyte, and
emits at most one frame every 33 ms. Intermediate states of a scrolling build
log are never rendered, because nobody can read them — this is the single
biggest reason the shell will feel fast.

The child writes nothing but frames and acks back. It gets terminal bytes
only — never a credential, a profile, or a vault handle. It cannot
authenticate anything.
"""

from __future__ import annotations

import time
from typing import Any

import pyte

from surftp.shell.protocol import ACK, FEED, FRAME, QUIT, RESIZE, MessageReader, frame

# Minimum interval between frames (seconds). 30 fps cap keeps IPC ~100 KB/s.
FRAME_INTERVAL_SECONDS: float = 1.0 / 30.0

DEFAULT_COLS: int = 120
DEFAULT_ROWS: int = 40


def _normalise(value: Any, default: int) -> int:
    """Clamp a dimension to a sane positive range."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, 1000))


def _flags(char: Any) -> int:
    """Pack pyte's boolean char attributes into a flags bitfield.

    Bit 0 bold, 1 italics, 2 underscore, 3 strikethrough, 4 reverse, 5 blink.
    The TerminalView mirrors this order when deciding how to render a run.
    """
    return (
        int(bool(getattr(char, "bold", False)))
        | int(bool(getattr(char, "italics", False))) << 1
        | int(bool(getattr(char, "underscore", False))) << 2
        | int(bool(getattr(char, "strikethrough", False))) << 3
        | int(bool(getattr(char, "reverse", False))) << 4
        | int(bool(getattr(char, "blink", False))) << 5
    )


def build_rows(screen: pyte.Screen) -> list[list[list]]:
    """Render the whole screen as compact run rows.

    A row is a list of runs ``[text, fg, bg, flags]``, one per style span.
    Runs only change when the style changes, so a plain screen collapses to
    one run per row. ``screen.buffer`` is a defaultdict keyed by row, and each
    row is itself a defaultdict keyed by column, so both are walked by index.
    """
    rows: list[list[list]] = []
    for y in range(screen.lines):
        line = screen.buffer[y]
        runs: list[list] = []
        text: list[str] = []
        fg = bg = None
        flags = 0
        for x in range(screen.columns):
            char = line[x]
            fl = _flags(char)
            if (char.fg != fg or char.bg != bg or fl != flags) and text:
                runs.append(["".join(text), fg, bg, flags])
                text = []
            fg, bg, flags = char.fg, char.bg, fl
            text.append(char.data)
        if text:
            runs.append(["".join(text), fg, bg, flags])
        rows.append(runs)
    return rows


def main(child_conn: Any) -> None:
    """Child-process entry point: read framed messages off the pipe, write frames.

    Exits on ``QUIT`` or on EOF from the parent (the parent died — a leaked
    emulator must not outlive its owner). ``child_conn`` is the child's end of
    the duplex pipe handed to us by the spawn context.
    """
    screen = pyte.Screen(DEFAULT_COLS, DEFAULT_ROWS)
    stream = pyte.ByteStream(screen)
    reader = MessageReader()
    state = {"seq": 0, "last_emit": 0.0, "dirty": False, "pending": bytearray()}

    def emit_frame() -> None:
        """Send the current screen as one frame and mark it clean."""
        child_conn.send_bytes(
            frame(
                {
                    "t": FRAME,
                    "seq": state["seq"],
                    "cursor": [screen.cursor.x, screen.cursor.y],
                    "rows": build_rows(screen),
                }
            )
        )
        state["seq"] += 1
        state["last_emit"] = time.monotonic()
        state["dirty"] = False

    try:
        while True:
            try:
                if not child_conn.poll(FRAME_INTERVAL_SECONDS):
                    # No new input: flush any dirty frame that is now due.
                    # Without this, a feed that arrives inside the coalescing
                    # window would leave a dirty screen sitting unrendered
                    # until more data arrived — a stalled-looking terminal.
                    if state["dirty"] and time.monotonic() - state["last_emit"] >= FRAME_INTERVAL_SECONDS:
                        emit_frame()
                    continue
                chunk = child_conn.recv_bytes()
            except (EOFError, OSError):
                return  # parent closed the pipe; a leaked child dies here
            for message in reader.feed(chunk):
                mtype = message.get("t")
                if mtype == QUIT:
                    return
                if mtype == RESIZE:
                    cols = _normalise(message.get("cols"), DEFAULT_COLS)
                    rows = _normalise(message.get("rows"), DEFAULT_ROWS)
                    screen.resize(lines=rows, columns=cols)
                    state["dirty"] = True
                elif mtype == FEED:
                    data = message.get("d", b"")
                    if isinstance(data, str):
                        data = data.encode("utf-8", "replace")
                    state["pending"].extend(data)
                    state["dirty"] = True

            if not state["dirty"]:
                continue

            # Coalesce: feed everything buffered into pyte, then emit at most
            # one frame per interval. The ack is the back-pressure signal that
            # lets the parent read the SSH channel only as fast as we consume.
            try:
                stream.feed(bytes(state["pending"]))
            except Exception:
                pass  # a bad escape sequence must never kill the emulator
            acked = len(state["pending"])
            state["pending"].clear()
            child_conn.send_bytes(frame({"t": ACK, "bytes": acked}))

            if time.monotonic() - state["last_emit"] >= FRAME_INTERVAL_SECONDS:
                emit_frame()
            # else: the frame stays pending; the poll above flushes it when due.
    finally:
        try:
            child_conn.close()
        except OSError:
            pass