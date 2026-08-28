# Plan: Basic Commander-Style UI

**Goal:** a runnable Textual app with two side-by-side file panes, a working key-binding layer, and
the left pane listing the current working directory. Local filesystem only — no SFTP/SSH yet.

**Explicitly out of scope for this pass:** any network transport, transfer engine, tunnel, or
connection dialog. This pass exists to make `python -m surftp` open a usable two-pane browser, so the
next pass can swap one pane's backend for a remote one without touching the widget code.

---

## 0. Prerequisites (fix before writing anything new)

These are broken today and will block the first `python -m surftp`:

| File | Problem | Fix |
| --- | --- | --- |
| `surftp/app.py` | `class SurfFTP()` has no colon, `compose()` has no body — the file does not parse | Rewritten entirely in step 3 |
| `surftp/__main__.py` | imports `incus_tui.__about__`, left over from another project | Rewrite to import from `surftp` |
| `surftp/__about__.py` | empty, while `__init__.py` holds `__version__` | Make `__about__.py` the single source of `__version__`; `__init__.py` re-exports it |

No new dependencies. Textual 8.2.8 is already pinned, and this pass touches nothing else.

---

## 1. Module layout

```
surftp/
  __about__.py      # __version__ — single source of truth
  __init__.py       # re-exports __version__
  __main__.py       # argparse entry point -> SurfFTPApp().run()
  app.py            # SurfFTPApp: layout, global bindings, pane focus
  bindings.py       # the key map, declared once as data
  widgets/
    __init__.py
    pane.py         # FilePane: one directory listing
  fs/
    __init__.py
    types.py        # FileEntry — the protocol-agnostic row model
    local.py        # LocalFileSystem — reads the real disk
  app.tcss          # layout + theme
```

The `fs/` split is the load-bearing part. `FilePane` must never import `os` or `pathlib` directly —
it renders whatever `FileEntry` list its filesystem hands back. That is the seam a remote backend
drops into later.

---

## 2. `fs/` — the filesystem seam

### `fs/types.py`

A frozen dataclass, protocol-neutral, no `Path` in the public surface (a remote path is a string, not
a local `Path`):

```python
@dataclass(frozen=True, slots=True)
class FileEntry:
    """A single row in a file pane, independent of which protocol produced it."""
    name: str          # basename only; ".." for the parent link
    path: str          # full path on the owning filesystem
    is_dir: bool
    size: int          # bytes; 0 for directories
    modified: float    # POSIX timestamp
```

Plus two small formatting helpers, each fully annotated and documented:

- `format_size(size: int, is_dir: bool) -> str` — `"<DIR>"` for directories, otherwise a human-readable
  byte count. Kept out of the widget so the remote backend renders identically.
- `format_modified(timestamp: float) -> str` — a fixed-width `YYYY-MM-DD HH:MM`, so columns align.

### `fs/local.py`

```python
class LocalFileSystem:
    """Reads the machine's own disk. The reference implementation of the pane backend."""

    def list_directory(self, path: str) -> list[FileEntry]: ...
    def parent_of(self, path: str) -> str: ...
    def is_directory(self, path: str) -> bool: ...
```

Behaviour to get right here, because every later backend copies it:

- **Sort order:** directories first, then files, each case-insensitively by name. Commander users rely
  on this.
- **Parent link:** prepend a `..` entry unless already at the filesystem root, so navigation needs no
  special key.
- **Errors do not propagate as crashes.** `PermissionError` / `FileNotFoundError` on the directory
  itself must raise a single typed `FileSystemError` that the pane catches and shows; the app must
  never die because a directory was unreadable. A per-entry `stat()` failure (a broken symlink) is
  skipped, not fatal.
- **Sync is fine for now** — local `listdir` is fast. Mark it, in a comment, as the thing that must
  become async or worker-backed when a network backend arrives, since that is the one rule that
  cannot be retrofitted cheaply.

Defining a `FileSystem` `typing.Protocol` here is worth it now: it costs three lines and makes
`FilePane`'s type annotation honest about accepting any backend.

---

## 3. Widgets and app

### `widgets/pane.py` — `FilePane`

A `Static` container wrapping a `DataTable[str]` with `cursor_type = "row"`, four columns
(Name / Size / Modified / Mode). Reactive `path: reactive[str]`, so assigning to it triggers a reload
via `watch_path`.

Public surface, every method annotated and docstringed:

- `__init__(self, filesystem: FileSystem, path: str, *, id: str | None = None) -> None`
- `load_directory(self) -> None` — clears the table and repopulates from the backend; catches
  `FileSystemError` and shows the message in the pane's border subtitle rather than raising.
- `watch_path(self, old: str, new: str) -> None` — reactive hook; reloads on any path change.
- `selected_entry(self) -> FileEntry | None` — the row under the cursor, or `None` on an empty pane.
- `open_selected(self) -> None` — descends into a directory (or follows `..`); a no-op on files for
  now, since there is no viewer yet.
- `refresh_listing(self) -> None` — re-reads the current path, preserving the cursor row where possible.

Each pane keeps its own path, so the two are fully independent. The **border title shows the pane's
current path** — that satisfies "the left pane shows the current working directory" and gives the
right pane the same affordance for free.

### `app.py` — `SurfFTPApp`

- `compose()` yields `Header()`, a `Horizontal` holding two `FilePane`s, then `Footer()`.
- The left pane starts at `Path.cwd()`; the right starts at the same place for now (it becomes the
  remote pane later).
- `on_mount()` focuses the left pane's table so the app is keyboard-usable immediately.
- `active_pane` / `inactive_pane` properties resolve which pane has focus. Every action routes through
  them — this is what makes one key binding work correctly on whichever side the user is on.
- Actions: `action_focus_next_pane`, `action_open_selected`, `action_go_parent`, `action_refresh`,
  `action_toggle_hidden`, `action_toggle_help`, `action_quit`.

Visual focus matters in a two-pane UI: style `FilePane:focus-within` with an accent border in
`app.tcss` so the active side is unmistakable.

---

## 4. `bindings.py` — the key map

Declared as one `BINDINGS` list of `Binding` objects, imported by `app.py`. Keeping it in its own
module means the key map can be printed, tested, and later made user-configurable without touching
app logic. `Binding` in Textual 8.2.8 accepts `key, action, description, show, key_display,
priority, tooltip, id, system, group` — use `tooltip` for the longer explanation and keep
`description` short enough for the footer.

| Key | Action | Description |
| --- | --- | --- |
| `tab` | `focus_next_pane` | Switch pane |
| `enter` | `open_selected` | Open directory |
| `backspace` | `go_parent` | Parent directory |
| `ctrl+r` / `f5` | `refresh` | Reload listing |
| `ctrl+h` | `toggle_hidden` | Show/hide dotfiles |
| `question_mark` | `toggle_help` | Key help panel |
| `ctrl+q` | `quit` | Quit |

`Footer()` renders every `show=True` binding automatically, so the help line is free. Bind `?` to
Textual's built-in help panel (`action_show_help_panel`) for the full list rather than hand-rolling a
modal.

Deliberately reserving, but **not** binding yet: `f2` rename, `f3` view, `f4` edit, `f5` copy,
`f6` move, `f7` mkdir, `f8` delete. Standard commander layout — note them as reserved in the module
docstring so the next pass does not bind them to something else. (`f5` doubles as refresh only until
copy exists, at which point refresh keeps `ctrl+r`.)

---

## 5. Code style

- `from __future__ import annotations` at the top of every module.
- Full annotations on every parameter and return, including `-> None`. Prefer precise types over
  `Any`: `list[FileEntry]`, `reactive[str]`, `DataTable[str]`.
- A docstring on every module, class, and method — what it does and *why it exists here*, which is the
  part that will not be obvious to the next reader.
- `frozen=True, slots=True` dataclasses for the data model; no mutable shared state between panes.
- No bare `except:`. Catch the specific OS errors at the `fs/` boundary and re-raise as
  `FileSystemError`, so the UI has exactly one exception type to handle.

---

## 6. Verification

`python -m surftp` opens two panes; the left shows the CWD listing with the path in its border title;
`tab` visibly moves focus; `enter` descends and `backspace` ascends on the focused side only; the
footer lists the bindings; `ctrl+q` exits cleanly. Navigate into `/root` (or any unreadable directory)
and confirm the app shows an error in the pane instead of crashing.

Run with `textual run --dev surftp.app:SurfFTPApp` plus `textual console` in a second terminal for
live logs.

---

## 7. What this sets up

When the SFTP backend lands, it implements the same three `FileSystem` methods and gets assigned to
the right pane's `filesystem`. No change to `FilePane`, the bindings, or the layout. The one thing
that *will* change: `load_directory` must move onto a Textual worker, because a remote listing can
hang — which is why step 2 flags it now.
