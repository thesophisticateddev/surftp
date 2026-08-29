"""Parent<->child message schema for the shell emulator process.

This is the *one* place the wire format lives, so both sides can be written
against a single source of truth. Messages are msgpack-encoded dicts with a
``t`` (type) key; unknown fields are forward-compatible, unknown types are
ignored.

Frames are sent as **runs** rather than cells: a run is ``[text, fg, bg,
flags]`` — a horizontal span sharing one style. Sending runs keeps a 120×40
frame in the low kilobytes, so a 30 fps cap is ~100 KB/s of IPC.

Security note: msgpack, not pickle. Pickle executes code on load; the child is
our own, but a fixed schema costs nothing and removes the question.
"""

from __future__ import annotations

import msgpack

# parent -> child
FEED = "feed"      # raw bytes off the SSH channel
RESIZE = "resize"  # {cols, rows}
QUIT = "quit"

# child -> parent
FRAME = "frame"    # {seq, cursor, rows}
ACK = "ack"        # {bytes}: how much of `feed` has been consumed


def encode(message: dict) -> bytes:
    """Pack one message dict into bytes for the pipe."""
    return msgpack.packb(message, use_bin_type=True)


def decode(data: bytes) -> dict:
    """Unpack one message dict from the pipe."""
    return msgpack.unpackb(data, raw=False)


class MessageReader:
    """Frame a byte stream into length-prefixed messages.

    The child and parent each own one end of the pipe; a stream of packed
    messages needs framing to know where one ends and the next begins. The
    frame is a 4-byte big-endian length prefix.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[dict]:
        """Append ``data`` and return any complete messages decoded from it."""
        self._buf.extend(data)
        messages: list[dict] = []
        while True:
            if len(self._buf) < 4:
                break
            (length,) = __import__("struct").unpack(">I", bytes(self._buf[:4]))
            if len(self._buf) < 4 + length:
                break
            payload = bytes(self._buf[4 : 4 + length])
            del self._buf[: 4 + length]
            messages.append(decode(payload))
        return messages


def frame(message: dict) -> bytes:
    """Encode one message with its length prefix, ready for the pipe."""
    payload = encode(message)
    return __import__("struct").pack(">I", len(payload)) + payload