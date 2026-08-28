# Results: Basic Commander-Style UI (plan `plan-commander-ui.md`)

**Date:** 2026-08-28
**Status:** Complete — all plan steps implemented and verified headlessly via Textual's `run_test()` pilot.

## What was built

Module layout exactly as specified in the plan:

```
surftp/
  __about__.py      # __version__ = "0.1.0" (single source of truth)
  __init__.py       # re-exports __version__
  __main__.py       # argparse entry -> SurfFTPApp().run()
  app.py            # SurfFTPApp: layout, global bindings, pane focus routing
  bindings.py       # BINDINGS list declared once as data
  widgets/
    __init__.py
    pane.py         # FilePane: protocol-blind directory listing
  fs/
    __init__.py
    types.py        # FileEntry (frozen dataclass) + format_size / format_modified
    local.py        # LocalFileSystem, FileSystem Protocol, FileSystemError
  app.tcss          # layout + focus-within accent border
```

- `app.py` invalid stub, `incus_tui` import, and the `__about__`/`__init__` version split — all prerequisite fixes done.
- Sorting: dirs first, then files, case-insensitive. `..` link prepended except at root.
- Errors: OS errors at the `fs/` boundary re-raised as `FileSystemError`; the pane shows the message in its border subtitle and never crashes. Broken symlinks (per-entry `stat` failure) are skipped.
- Panes are fully independent (own path, own backend instance); border title shows each pane's current path.
- `app.py` routes every action through `active_pane` / `inactive_pane` properties.
- Bindings: tab / enter / backspace / ctrl+r (+f5 alias) / ctrl+h / ? / ctrl+q. F2–F8 commander keys documented as reserved in `bindings.py`'s docstring.

## Deviations / discoveries (Textual 8.2.8 specifics)

1. **`enter` is shadowed by DataTable.** The focused `DataTable` binds `enter` to its own `select_cursor`, so the app-level binding never fires. Fixed with `priority=True` on the `enter` binding in `bindings.py`.
2. **Reactive `init=True` double-load.** Default reactives fire their watchers once at mount *in addition to* any explicit set in `on_mount`, which produced duplicated rows (Textual's `clear()` inside a mount-time watcher did not dedupe). Fixed with `reactive(..., init=False)` on `path` / `show_hidden`; the initial load happens once via the explicit set in `on_mount`.
3. **`DataTable.cursor_row` is read-only** in 8.2.8; cursor restoration in `refresh_listing()` uses `move_cursor(row=...)` instead.
4. Rows are not added with `key=`; the pane keeps its own `_entries` list and maps `cursor_row` to it — avoids `DuplicateKey` edge cases and makes `selected_entry()` O(1).

## Verification performed

- `python -m surftp --version` → `surftp 0.1.0`; `python -m compileall surftp` clean.
- Headless pilot test (`app.run_test()`) verified: both panes list the CWD with path in border title; left table focused on mount; `enter` follows `..`/descends into a directory; `backspace` ascends; `tab` moves focus and `active_pane`/`inactive_pane` flip; `ctrl+h` toggles dotfiles (row count grows/shrinks, `.git`/`.plans` appear); `ctrl+r` reload preserves cursor; navigating to `/root` (unreadable as this user) shows `Error: ...` in the border subtitle instead of crashing; `question_mark` opens the help panel.

## Out of scope (per plan)

No network transport, transfers, tunnel, or connection dialog. Local filesystem only. When the SFTP backend lands it implements the same three `FileSystem` methods; `load_directory` must then move onto a Textual worker (flagged in `fs/local.py`).
