"""TerminalView: paints emulator frames and translates key events into bytes.

This widget is deliberately stateless about the terminal itself. It holds the
last frame the child sent (rows of style runs plus a cursor position) and is a
*display* for that frame; the pyte emulator lives in a child process (see
``surftp.shell.emulator``). That is what keeps it cheap — painting a run
strip is far cheaper than re-emulating the stream.

Key handling lives here in one table. The app's own bindings would otherwise
shadow the shell (``tab``, ``ctrl+d``, ``escape``, …), so the app implements
``check_action`` to disable pane/transfer actions while this widget is
focused, letting the raw key fall through to :meth:`on_key`.
"""

from __future__ import annotations

import asyncio

from rich.style import Style
from rich.text import Text
from textual import events
from textual.reactive import reactive
from textual.widget import Widget

from surftp.shell.session import ShellSession

# Bit positions mirroring the emulator's `_flags` packing.
FLAG_BOLD = 1
FLAG_ITALICS = 2
FLAG_UNDERSCORE = 4
FLAG_STRIKETHROUGH = 8
FLAG_REVERSE = 16
FLAG_BLINK = 32

# ANSI colour names pyte uses, mapped to Textual/rich theme names. "default"
# means "leave alone" so a terminal app can't paint over the TUI's own theme.
_ANSI_TO_RICH: dict[str, str | None] = {
    "default": None,
    "black": "black",
    "red": "red",
    "green": "green",
    "yellow": "yellow",
    "blue": "blue",
    "magenta": "magenta",
    "cyan": "cyan",
    "white": "white",
    "bright_black": "grey62",
    "bright_red": "bright_red",
    "bright_green": "bright_green",
    "bright_yellow": "bright_yellow",
    "bright_blue": "bright_blue",
    "bright_magenta": "bright_magenta",
    "bright_cyan": "bright_cyan",
    "bright_white": "bright_white",
}


class TerminalView(Widget):
    """Displays the latest emulator frame for one remote shell."""

    can_focus = True

    # The shell this view displays; assigned by the bottom panel when opened.
    shell: ShellSession | None = None

    _rows: reactive[list[list[list]]] = reactive([], init=False)
    _cursor: reactive[list[int]] = reactive([0, 0], init=False)

    def __init__(self, **kwargs) -> None:
        """Create an empty terminal view."""
        super().__init__(**kwargs)
        self._pending_input: list[bytes] = []

    # ------------------------------------------------------------------
    # Frame display
    # ------------------------------------------------------------------

    def set_frame(self, message: dict) -> None:
        """Paint the latest frame from the emulator child."""
        self._rows = message.get("rows", [])
        self._cursor = message.get("cursor", [0, 0])
        self.refresh()

    def render(self) -> Text:
        """Render the current frame as a rich Text.

        Each row is built from style runs; the cursor cell is drawn in reverse
        video. Rows beyond the emulator's height render blank, so a tiny
        terminal still fills the widget.
        """
        lines: list[Text] = []
        rows = self._rows
        for y in range(self.size.height if self.size else 24):
            if y < len(rows):
                lines.append(self._render_row(rows[y], y))
            else:
                lines.append(Text(" " * (self.size.width if self.size else 80)))
        return Text("\n").join(lines)

    def _render_row(self, runs: list[list], y: int) -> Text:
        """Render one row of runs into a Text, applying the cursor cell."""
        line = Text()
        col = 0
        for rtext, fg, bg, flags in runs:
            text = str(rtext)
            style = self._style_for(fg, bg, int(flags or 0))
            if (
                self._cursor
                and y == self._cursor[1]
                and col <= self._cursor[0] < col + len(text)
            ):
                # Reverse-video the cursor cell.
                ci = self._cursor[0] - col
                if ci > 0:
                    line.append(text[:ci], style)
                cursor_style = Style(
                    reverse=True,
                    color=_map_color(bg),
                    bgcolor=_map_color(fg),
                    bold=bool(int(flags or 0) & FLAG_BOLD),
                )
                line.append(text[ci], cursor_style)
                if ci + 1 < len(text):
                    line.append(text[ci + 1 :], style)
            else:
                line.append(text, style)
            col += len(text)
        return line

    def _style_for(self, fg, bg, flags: int) -> Style:
        """Build a rich Style from a run's fg/bg/flags."""
        return Style(
            color=_map_color(fg),
            bgcolor=_map_color(bg),
            bold=bool(flags & FLAG_BOLD),
            italic=bool(flags & FLAG_ITALICS),
            underline=bool(flags & FLAG_UNDERSCORE),
            strike=bool(flags & FLAG_STRIKETHROUGH),
            reverse=bool(flags & FLAG_REVERSE),
            blink=bool(flags & FLAG_BLINK),
        )

    # ------------------------------------------------------------------
    # Keyboard input
    # ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        """Translate a key event into bytes and send them to the shell."""
        if self.shell is None:
            return
        data = _key_to_bytes(event)
        if data:
            asyncio.create_task(self.shell.send_input(data))
        event.stop()

    async def on_resize(self, event: events.Resize) -> None:
        """Propagate the new size to both the remote terminal and the emulator."""
        if self.shell is None:
            return
        await self.shell.resize(event.size.width, event.size.height)


def _map_color(name) -> str | None:
    """Map a pyte colour name to a rich colour name, or None for default."""
    return _ANSI_TO_RICH.get(str(name).lower())


def _key_to_bytes(event: events.Key) -> bytes:
    """Translate a Textual key event into the bytes a remote shell expects.

    Printable characters go as UTF-8; control keys become their xterm escape
    sequences. Ctrl+letter becomes the control byte. This is the one place the
    mapping lives, so the shell behaves like a real terminal.
    """
    key = event.key
    char = event.character

    if char is not None and len(key) == 1:
        return char.encode("utf-8")

    specials = {
        "enter": b"\r",
        "backspace": b"\x7f",
        "delete": b"\x1b[3~",
        "tab": b"\t",
        "left": b"\x1b[D",
        "right": b"\x1b[C",
        "up": b"\x1b[A",
        "down": b"\x1b[B",
        "home": b"\x1b[H",
        "end": b"\x1b[F",
        "pageup": b"\x1b[5~",
        "pagedown": b"\x1b[6~",
        "insert": b"\x1b[2~",
        "f1": b"\x1bOP",
        "f2": b"\x1bOQ",
        "f3": b"\x1bOR",
        "f4": b"\x1bOS",
        "f5": b"\x1b[15~",
        "f6": b"\x1b[17~",
        "f7": b"\x1b[18~",
        "f8": b"\x1b[19~",
        "f9": b"\x1b[20~",
        "f10": b"\x1b[21~",
        "f11": b"\x1b[23~",
        "f12": b"\x1b[24~",
    }
    if key in specials:
        return specials[key]

    # Ctrl+letter -> control byte (e.g. ctrl+c -> \x03, ctrl+d -> \x04).
    if key.startswith("ctrl+") and len(key) == 6:
        letter = key[5].lower()
        if "a" <= letter <= "z":
            return bytes([ord(letter) - ord("a") + 1])

    # Space and printable single chars without a `character` reported.
    if key == "space":
        return b" "
    if len(key) == 1:
        return key.encode("utf-8")

    return b""