"""Unit tests for the SSH shell components: protocol, emulator, key translation.

No network, no Textual app — the emulator child is exercised through a real
spawned process (which requires running as a file, not via pytest's collect).
These mirror ``tests/test_shell_integration.py`` which covers the live SSH
path.

Note on pytest: the emulator is spawned with the ``spawn`` context, which
needs an importable ``__main__``. Running under pytest works because this
module imports fine; spawning from a pytest worker also works as long as the
event loop runs in the main thread.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import time

import pytest

from surftp.shell import emulator
from surftp.shell.protocol import (
    ACK,
    FEED,
    FRAME,
    QUIT,
    RESIZE,
    MessageReader,
    decode,
    encode,
    frame,
)
from surftp.widgets.terminal import _key_to_bytes
from textual.events import Key


class TestProtocol:
    """Message schema round-trips."""

    def test_encode_decode_roundtrip(self):
        msg = {"t": FEED, "d": b"hello"}
        assert decode(encode(msg)) == msg

    def test_frame_roundtrip(self):
        """Length-prefixed frames decode to the original message."""
        messages = [
            {"t": FEED, "d": b"\x1b[31mred"},
            {"t": RESIZE, "cols": 80, "rows": 24},
            {"t": QUIT},
        ]
        reader = MessageReader()
        for msg in messages:
            for decoded in reader.feed(frame(msg)):
                assert decoded == msg

    def test_multiple_frames_in_one_feed(self):
        """A single pipe chunk can carry several messages."""
        chunk = frame({"t": FEED, "d": b"a"}) + frame({"t": ACK, "bytes": 1})
        reader = MessageReader()
        decoded = reader.feed(chunk)
        assert len(decoded) == 2
        assert decoded[0]["t"] == FEED
        assert decoded[1]["t"] == ACK

    def test_partial_frame_is_buffered(self):
        """A truncated frame is held until the rest arrives."""
        full = frame({"t": FEED, "d": b"xyz"})
        reader = MessageReader()
        assert reader.feed(full[:3]) == []
        assert reader.feed(full[3:]) == [{"t": FEED, "d": b"xyz"}]


class TestEmulator:
    """The child process: pyte + coalescing, exercised via a real spawn."""

    def _spawn(self):
        ctx = multiprocessing.get_context("spawn")
        parent, child = ctx.Pipe(duplex=True)
        proc = ctx.Process(target=emulator.main, args=(child,), daemon=True)
        proc.start()
        child.close()
        return proc, parent

    def _drain(self, parent, reader, duration: float):
        """Read frames/acks for ``duration`` seconds."""
        frames, acks = [], []
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            while parent.poll(0.05):
                data = parent.recv_bytes()
                for m in reader.feed(data):
                    if m.get("t") == FRAME:
                        frames.append(m)
                    elif m.get("t") == ACK:
                        acks.append(m)
        return frames, acks

    def test_plain_text_frame(self):
        proc, parent = self._spawn()
        reader = MessageReader()
        try:
            parent.send_bytes(frame({"t": FEED, "d": b"hello world\n"}))
            frames, acks = self._drain(parent, reader, 0.5)
            assert acks, "no ack"
            text = "".join(r[0] for f in frames for row in f["rows"] for r in row)
            assert "hello world" in text
        finally:
            parent.send_bytes(frame({"t": QUIT}))
            proc.join(timeout=2)

    def test_colour_runs(self):
        proc, parent = self._spawn()
        reader = MessageReader()
        try:
            parent.send_bytes(frame({"t": FEED, "d": b"\x1b[31mred\x1b[0m plain\n"}))
            frames, _ = self._drain(parent, reader, 0.5)
            assert frames
            last = frames[-1]
            # A red run and a default run on the same row.
            red_run = next(
                (r for row in last["rows"] for r in row if r[1] == "red"), None
            )
            assert red_run is not None, last["rows"]
            assert red_run[0] == "red"
        finally:
            parent.send_bytes(frame({"t": QUIT}))
            proc.join(timeout=2)

    def test_resize_midstream(self):
        proc, parent = self._spawn()
        reader = MessageReader()
        try:
            parent.send_bytes(frame({"t": RESIZE, "cols": 40, "rows": 12}))
            frames, _ = self._drain(parent, reader, 0.4)
            resized = [f for f in frames if len(f["rows"]) == 12]
            assert resized, "no frame reflected the resize"
        finally:
            parent.send_bytes(frame({"t": QUIT}))
            proc.join(timeout=2)

    def test_at_most_one_frame_per_interval(self):
        """Under a continuous feed, the child coalesces to ~30 fps."""
        proc, parent = self._spawn()
        reader = MessageReader()
        try:
            # Send a burst of feeds.
            for i in range(50):
                parent.send_bytes(frame({"t": FEED, "d": b"line\n"}))
            frames, _ = self._drain(parent, reader, 0.6)
            # 0.6s at 30 fps allows ~18 frames; a child that emitted per-feed
            # would produce ~50. Allow generous headroom for CI timing.
            assert len(frames) <= 40, f"too many frames: {len(frames)}"
            assert len(frames) >= 1
        finally:
            parent.send_bytes(frame({"t": QUIT}))
            proc.join(timeout=2)

    def test_quit_stops_the_child(self):
        proc, parent = self._spawn()
        parent.send_bytes(frame({"t": QUIT}))
        proc.join(timeout=2)
        assert not proc.is_alive(), "emulator did not exit on QUIT"

    def test_parent_eof_stops_the_child(self):
        """A closed parent pipe must not leave a leaked child."""
        proc, parent = self._spawn()
        parent.close()
        proc.join(timeout=2)
        assert not proc.is_alive(), "emulator outlived its parent"


class TestKeyTranslation:
    """The one key map between Textual events and shell bytes."""

    def _key(self, key: str, character=None):
        return _key_to_bytes(Key(key=key, character=character))

    def test_printable(self):
        assert self._key("a", "a") == b"a"
        assert self._key("space", " ") == b" "

    def test_enter_and_backspace(self):
        assert self._key("enter") == b"\r"
        assert self._key("backspace") == b"\x7f"

    def test_arrows(self):
        assert self._key("up") == b"\x1b[A"
        assert self._key("down") == b"\x1b[B"
        assert self._key("left") == b"\x1b[D"
        assert self._key("right") == b"\x1b[C"

    def test_home_end_page(self):
        assert self._key("home") == b"\x1b[H"
        assert self._key("end") == b"\x1b[F"
        assert self._key("pageup") == b"\x1b[5~"
        assert self._key("pagedown") == b"\x1b[6~"

    def test_ctrl_letters(self):
        assert self._key("ctrl+c") == b"\x03"
        assert self._key("ctrl+d") == b"\x04"
        assert self._key("ctrl+l") == b"\x0c"

    def test_f_keys(self):
        assert self._key("f1") == b"\x1bOP"
        assert self._key("f10") == b"\x1b[21~"

    def test_unknown_keys_are_empty(self):
        assert self._key("f13") == b""
        assert self._key("ctrl+pageup") == b""


if __name__ == "__main__":
    # Direct runner (spawn-safe as a file).
    async def run_async():
        results = []
        for cls in (TestProtocol, TestKeyTranslation):
            for name in dir(cls):
                if name.startswith("test_"):
                    method = getattr(cls(), name)
                    if asyncio.iscoroutinefunction(method):
                        await method()
                    else:
                        method()
                    results.append(f"  PASS  {cls.__name__}.{name}")
        for name in dir(TestEmulator):
            if name.startswith("test_"):
                getattr(TestEmulator(), name)()
                results.append(f"  PASS  TestEmulator.{name}")
        print("\n".join(results))
        print(f"\nALL {len(results)} SHELL UNIT TESTS PASSED")

    asyncio.run(run_async())