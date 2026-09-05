"""The SURFTP key map, declared once as data.

Keeping bindings in their own module means the key map can be printed,
tested, and later made user-configurable without touching app logic.

Reserved for the standard commander layout, deliberately NOT bound yet:
f2 rename, f3 view, f4 edit, f7 mkdir, f8 delete.
``f9`` is taken by Connect, which sits just past that reserved block.
"""

from __future__ import annotations

from textual.binding import Binding

BINDINGS: list[Binding] = [
    # priority=True for the same reason as `enter`: without it Textual's own
    # focus-next handling consumes the key first, and focus cycles into any
    # other focusable widget (the transfer panel's table) instead of switching
    # sides. It only looked correct while the two pane tables were the only
    # focusable widgets in the tree.
    Binding("tab", "focus_next_pane", "Switch pane", show=True, priority=True,
            tooltip="Move keyboard focus to the other pane"),
    Binding("enter", "open_selected", "Open dir", show=True, priority=True,
            tooltip="Descend into the selected directory (or follow ..)"),
    Binding("backspace", "go_parent", "Parent", show=True,
            tooltip="Go to the parent directory of the focused pane"),
    Binding("ctrl+r", "refresh", "Reload", show=True,
            tooltip="Re-read the current listing of the focused pane"),
    Binding("f5", "copy_to_other", "Copy", show=True,
            tooltip="Copy the selected file(s) to the other pane"),
    Binding("f6", "move_to_other", "Move", show=True,
            tooltip="Move the selected file(s) to the other pane (copy then delete source)"),
    Binding("ctrl+h", "toggle_hidden", "Hidden", show=True,
            tooltip="Show or hide dotfiles in the focused pane"),
    Binding("ctrl+t", "toggle_transfers", "Transfers", show=True,
            tooltip="Show or hide the transfer queue panel"),
    Binding("escape", "cancel_transfer", "Cancel", show=False,
            tooltip="Cancel the running transfer (with confirmation if multiple files remain)"),
    Binding("question_mark", "toggle_help", "Help", show=True,
            tooltip="Show the key bindings help panel"),
    Binding("f9", "connect", "Connect", show=True,
            tooltip="Connect the focused pane to a remote host"),
    Binding("ctrl+o", "open_profiles", "Profiles", show=True,
            tooltip="Choose a saved connection profile"),
    Binding("ctrl+d", "disconnect", "Disconnect", show=True,
            tooltip="Close the active session tab (disconnects it)"),
    # priority=True on the session keys for the same reason as `tab`: the
    # focused DataTable and its scroll container claim the page keys first.
    Binding("ctrl+w", "close_session", "Close tab", show=False,
            tooltip="Close the active session tab (alias for ctrl+d)", priority=True),
    Binding("ctrl+pageup", "previous_session", "Prev tab", show=True,
            tooltip="Switch to the previous session tab on this side", priority=True),
    Binding("ctrl+pagedown", "next_session", "Next tab", show=True,
            tooltip="Switch to the next session tab on this side", priority=True),
    Binding("alt+1", "activate_session_1", "Tab 1", show=False,
            tooltip="Jump to session tab 1 (Local)"),
    Binding("alt+2", "activate_session_2", "Tab 2", show=False,
            tooltip="Jump to session tab 2"),
    Binding("alt+3", "activate_session_3", "Tab 3", show=False,
            tooltip="Jump to session tab 3"),
    Binding("alt+4", "activate_session_4", "Tab 4", show=False,
            tooltip="Jump to session tab 4"),
    Binding("alt+5", "activate_session_5", "Tab 5", show=False,
            tooltip="Jump to session tab 5"),
    Binding("alt+6", "activate_session_6", "Tab 6", show=False,
            tooltip="Jump to session tab 6"),
    Binding("alt+7", "activate_session_7", "Tab 7", show=False,
            tooltip="Jump to session tab 7"),
    Binding("alt+8", "activate_session_8", "Tab 8", show=False,
            tooltip="Jump to session tab 8"),
    Binding("alt+9", "activate_session_9", "Tab 9", show=False,
            tooltip="Jump to session tab 9"),
    Binding("ctrl+l", "lock_vault", "Lock", show=False,
            tooltip="Lock the credential vault, forgetting the master password"),
    Binding("f10", "focus_panes", "Panes", show=False, priority=True,
            tooltip="Return focus to the file panes (escape hatch from a shell)"),
    Binding("ctrl+enter", "open_shell", "Shell", show=True,
            tooltip="Open an interactive shell on the focused session tab"),
    Binding("f11", "connections", "Connections", show=True,
            tooltip="Show the live SSH connections panel"),
    Binding("ctrl+q", "quit", "Quit", show=True,
            tooltip="Exit SURFTP"),
]
