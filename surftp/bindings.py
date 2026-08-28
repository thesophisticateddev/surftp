"""The SURFTP key map, declared once as data.

Keeping bindings in their own module means the key map can be printed,
tested, and later made user-configurable without touching app logic.

Reserved for the standard commander layout, deliberately NOT bound yet:
f2 rename, f3 view, f4 edit, f5 copy, f6 move, f7 mkdir, f8 delete.
``f9`` is taken by Connect, which sits just past that reserved block.
(``f5`` doubles as refresh only until copy exists, at which point refresh
keeps ``ctrl+r``.)
"""

from __future__ import annotations

from textual.binding import Binding

BINDINGS: list[Binding] = [
    Binding("tab", "focus_next_pane", "Switch pane", show=True,
            tooltip="Move keyboard focus to the other pane"),
    Binding("enter", "open_selected", "Open dir", show=True, priority=True,
            tooltip="Descend into the selected directory (or follow ..)"),
    Binding("backspace", "go_parent", "Parent", show=True,
            tooltip="Go to the parent directory of the focused pane"),
    Binding("ctrl+r", "refresh", "Reload", show=True,
            tooltip="Re-read the current listing of the focused pane"),
    Binding("f5", "refresh", "Reload", show=False,
            tooltip="Alias for reload; reserved for copy once transfers exist"),
    Binding("ctrl+h", "toggle_hidden", "Hidden", show=True,
            tooltip="Show or hide dotfiles in the focused pane"),
    Binding("question_mark", "toggle_help", "Help", show=True,
            tooltip="Show the key bindings help panel"),
    Binding("f9", "connect", "Connect", show=True,
            tooltip="Connect the focused pane to a remote host"),
    Binding("ctrl+o", "open_profiles", "Profiles", show=True,
            tooltip="Choose a saved connection profile"),
    Binding("ctrl+d", "disconnect", "Disconnect", show=True,
            tooltip="Disconnect the focused pane and return it to the local disk"),
    Binding("ctrl+l", "lock_vault", "Lock", show=False,
            tooltip="Lock the credential vault, forgetting the master password"),
    Binding("ctrl+q", "quit", "Quit", show=True,
            tooltip="Exit SURFTP"),
]
