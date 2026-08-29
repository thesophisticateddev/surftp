# Plan: Session Tabs and a Permissions Column

**Goal:** two independent pane improvements.

* **Part A — permissions.** Show real POSIX permissions (`drwxr-xr-x`) in both panes, for every
  backend that reports them.
* **Part B — session tabs.** A tab bar above each pane so one side can hold several sessions at once
  — a permanent **Local** tab plus one tab per remote connection — and the user switches between them
  instead of losing the local view every time they connect.

**Out of scope:** changing permissions (a `chmod` dialog), owner/group columns, persisting open tabs
across restarts, and dragging a tab from one side to the other.

They are shipped in this order because **Part A is independent and carries almost no risk**, while
Part B changes how the app resolves "which pane is the left pane". Landing A first means the bigger
change is not also carrying a data-model change.

---

## Part A — Permissions column

### A1. Why this is nearly free

Both backends already compute the mode and throw it away: `fs/local.py:94` takes `st.st_mode` only to
test `S_ISDIR`, and `net/sftp.py:90` does the same with `attrs.permissions`. The pane already has a
**Mode** column, currently rendering the single character `d` or `-`. So this is: carry a number that
already exists through `FileEntry`, and render it properly.

### A2. `FileEntry` gains one field

```python
@dataclass(frozen=True, slots=True)
class FileEntry:
    name: str
    path: str
    is_dir: bool
    size: int
    modified: float
    permissions: int = 0     # full POSIX st_mode where known; 0 = "backend did not say"
```

**The default is what protects existing code.** Adding a trailing field with a default keeps every
current `FileEntry(...)` construction valid, including the `..` links and the transfer planner's
items. No call site *has* to change; the ones that can supply a mode simply do.

Store the **full `st_mode`**, not just the low 9 bits: it carries the file-type bits, which is how the
formatter distinguishes a symlink from a regular file without a second field.

### A3. `format_permissions(permissions: int, is_dir: bool) -> str`

Lives in `fs/types.py` beside `format_size` / `format_modified`, so every backend renders identically.

* Returns the familiar 10 characters: type char + `rwx` triplets, e.g. `drwxr-xr-x`, `-rw-r--r--`.
* Honours setuid/setgid/sticky (`rws`, `rwt`) — a visible setuid bit is exactly the kind of thing an
  admin opens a file browser to check.
* Type char from `stat.S_IFMT` when the mode carries type bits, else `d`/`-` from `is_dir`.
* `permissions == 0` renders `""` (blank), not `----------`. A blank cell says "unknown"; ten dashes
  falsely asserts "no permissions at all", and FTP servers that omit the fact would make every file
  look unreadable.
* The `..` row keeps `permissions=0` and so renders blank — it is a navigation affordance, not a file.

### A4. Backend changes

| Backend | Source | Note |
| --- | --- | --- |
| `LocalFileSystem` | `st.st_mode` (already in hand) | one-line change in both list and stat paths |
| `SFTPFileSystem` | `attrs.permissions` (already in hand) | one-line change |
| `FTPFileSystem` | `info["unix.mode"]` | **needs care — see below** |

**The FTP trap.** `aioftp` gives `unix.mode` from two different paths with two different types. Its
`LIST` parser (`parse_unix_mode`) returns a proper numeric mode. But `MLSD` yields the server's raw
fact, which is *octal text* such as `"0644"` — and `aioftp`'s `UnixListInfo` TypedDict annotates it as
`int`, so the mistake reads as correct. `int("0644")` is 644 decimal = `0o1204`, i.e. nonsense
permissions displayed with total confidence. Parse defensively:

```python
def _parse_mode(value: object) -> int:
    """Read a mode from an aioftp listing fact.

    MLSD delivers octal *text* ("0644") while aioftp's LIST parser delivers an
    already-numeric mode. Guessing wrong renders plausible-looking nonsense, so
    strings are parsed base 8 and ints trusted as-is.
    """
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value, 8)
        except ValueError:
            return 0
    return 0
```

### A5. Pane rendering

`pane.py` swaps its `"d" if entry.is_dir else "-"` cell for
`format_permissions(entry.permissions, entry.is_dir)`. The column set does not change — still
`Name / Size / Modified / Mode` — so no layout work and no test churn.

One sizing note: the Mode column is now 10 characters wide. Give `Name` an explicit flexible width so
a narrow terminal squeezes the mode column rather than truncating file names, which are what the user
is actually reading.

### A6. Verifying Part A

* Unit: `format_permissions` against known modes — `0o40755` → `drwxr-xr-x`, `0o100644` →
  `-rw-r--r--`, `0o104755` → `-rwsr-xr-x`, `0o41777` → `drwxrwxrwt`, `0o120777` → `lrwxrwxrwx`,
  `0` → `""`.
* `_parse_mode("0644") == 0o644` and `_parse_mode(420) == 420` — the exact confusion above.
* Integration: create files `0o600`, `0o755` and a `0o4755` setuid file in the SSH test server's
  directory, list them over SFTP, and assert the rendered strings. Same over FTP against the aioftp
  server, which exercises the MLSD path.
* Pilot: the Mode column shows a 10-character string for a real local file.

---

## Part B — Session tabs

### B1. What changes for the user

Today `f9` **replaces** the focused pane's backend, so the local directory you were standing in is
gone until you disconnect. After this change:

* Each side has a tab bar. Tab 1 is always **Local** and cannot be closed.
* `f9` (and picking a saved profile) opens a **new tab** on the focused side and activates it. The
  Local tab is untouched and still sitting where you left it.
* Several remote sessions can be open at once, per side, each with its own connection and path.
* Closing a session tab disconnects it; `ctrl+d` keeps working and now means "close this session tab".

### B2. The compatibility contract — the single hinge

`app.py` currently resolves panes by DOM id:

```python
return self.query_one("#left", FilePane)
```

With several panes per side those ids stop being unique. **The fix is to change only the resolution,
never the contract.** `left_pane` / `right_pane` keep their names, their type, and their meaning ("the
pane the user is looking at on that side") and start returning *the active tab's* `FilePane`:

```python
@property
def left_pane(self) -> FilePane:
    """The pane the user currently sees on the left — now the active session tab's."""
    return self.left_sessions.active_pane
```

Everything downstream — `active_pane`, `inactive_pane`, every `action_*`, the connect flow, the
transfer flow, `close_everything` — continues to work **unmodified**, because all of it is already
written in terms of those two properties. This is the property that makes the feature safe, and it is
the reason `active_pane`/`inactive_pane` were introduced in the first pass.

Per-pane ids become `left-0`, `left-1`, … Nothing outside `app.py` queries `#left` / `#right`; the
test suites go through `app.left_pane`, so they keep passing as written.

### B3. New widget: `surftp/widgets/sessions.py`

```python
class SessionTabs(Vertical):
    """One side of the commander view: a tab bar over a stack of FilePanes.

    Owns the panes for its side. The app asks it which pane is active and never
    reaches past it into the tab machinery.
    """

    side: str                                    # "left" | "right"

    @property
    def active_pane(self) -> FilePane: ...
    @property
    def panes(self) -> list[FilePane]: ...       # for shutdown and "is a transfer running?"

    async def open_session(self, connection: RemoteConnection) -> FilePane: ...
    async def close_active_session(self) -> bool: ...   # False if it is the Local tab
    def activate(self, index: int) -> None: ...
    def next_session(self) -> None: ...
    def previous_session(self) -> None: ...
```

Built on Textual's `TabbedContent` / `TabPane` (both present in Textual 8.2.8, with `add_pane`,
`remove_pane`, `active`, and a `TabActivated` message). One `TabPane` wraps one `FilePane`.

Tab titles: `Local` for tab 1; for a session, the profile name when it has one, else `user@host`.
Truncate to ~18 characters — a long profile name must not push the tab bar into a scroll.

### B4. Behaviour that is easy to get wrong

* **Focus must land on the table, never the tab bar.** `TabbedContent` inserts a focusable `Tabs`
  widget into the focus chain, and the app binds `tab` to *switch panes*. If focus lands on the tab
  bar, `enter`/arrow keys go to the wrong widget and the app feels broken. Set the underlying `Tabs`
  to `can_focus = False`, and on every `TabActivated` explicitly focus the new pane's `DataTable`.
* **`active_pane` must not be fooled by the tab bar.** It currently tests
  `right_pane.has_focus_within`. Ask the *side container* instead (`SessionTabs.has_focus_within`) and
  keep a `_last_active_side` fallback for the moment when focus is briefly nowhere — during a modal,
  for instance, which is exactly when the connect flow needs to know which side to attach to.
* **Never close a tab with a transfer in flight.** The engine holds a reference to the pane's
  `FileSystem`, not the widget, so a transfer *survives* a tab switch — which is correct and worth
  keeping. But closing the tab tears down the connection under a running job. Refuse with a
  notification, or offer to cancel first.
* **Shutdown must walk every tab.** `close_everything` iterates the two panes today; it becomes a walk
  over `left_sessions.panes + right_sessions.panes`. A missed tab is a leaked SSH connection at exit.
* **A dropped connection closes its tab, not the app.** If a session's transport dies, mark the tab
  (e.g. a `✕` prefix) and let the user close it; do not auto-remove a tab out from under the cursor.
* **Tab count needs a ceiling.** Each session is a live SSH connection. Cap at 8 per side and say why
  when refusing — an accidental key-repeat on `f9` should not open forty connections.

### B5. Connect flow changes

In `app.py`, exactly one behavioural edit: `attach()` currently calls
`pane.attach_connection(connection)` on the active pane. It becomes
`await side.open_session(connection)`, which creates the tab, builds the `FilePane` already bound to
that connection, activates it, and focuses its table.

`FilePane.attach_connection` / `detach_connection` stay as they are — a pane created for a session is
constructed with its backend, and the Local tab's pane never has a connection attached. Keeping those
methods intact means the "disconnect returns this pane to local" path still exists for the one case
that still uses it: a session tab that the user closes while it is the only tab.

### B6. New bindings

Checked against every existing binding (`tab`, `enter`, `backspace`, `ctrl+r`, `f5`, `f6`, `ctrl+h`,
`ctrl+t`, `escape`, `?`, `f9`, `ctrl+o`, `ctrl+d`, `ctrl+l`, `ctrl+q`) — no collisions:

| Key | Action | Description |
| --- | --- | --- |
| `ctrl+pagedown` | `next_session` | Next session tab on the focused side |
| `ctrl+pageup` | `previous_session` | Previous session tab |
| `alt+1` … `alt+9` | `activate_session(n)` | Jump straight to tab *n* |
| `ctrl+w` | `close_session` | Close the focused session tab (disconnects it) |

`ctrl+d` stays bound to `disconnect` and is redefined as an alias of `close_session`, so existing
muscle memory — and `test_ui_connect.py`, which presses it — keeps working.

### B7. Why the existing tests still pass, checked case by case

This is the part worth being explicit about, since "do not break existing functionality" is the
requirement:

* `test_ui.py` takes `app.left_pane` / `app.right_pane` and drives navigation, hidden-file toggling
  and error display. All of it runs against the Local tab, which behaves exactly as the single pane
  does today.
* `test_ui_connect.py:102` asserts `left.is_remote` after connecting. With tabs, connecting opens a
  new tab **and activates it**, so `app.left_pane` is that session's pane → still true.
* `:107` asserts the other side stayed local → unchanged, connecting never touches the other side.
* `:123` asserts `not left.is_remote` after `ctrl+d` → closing the session tab activates the Local tab
  → still true.
* `:124` asserts the pane returned to the previous local path. Today that relies on `_local_path`
  being restored; with tabs the Local tab **never left** that path, so the assertion holds more
  strongly than before.
* `:133`, `:144`, `:176` use `app.active_pane` → unchanged by construction.
* The vault, integration, and FTP suites do not touch the UI at all.

**Requirement: all five existing suites pass unmodified.** If a suite needs editing to accommodate
tabs, that is a signal the contract in B2 was broken — not a licence to edit the test.

### B8. Verifying Part B

* Pilot: two sessions open on one side; `ctrl+pagedown` cycles between them and the listing changes
  accordingly; the Local tab still shows the original directory; `alt+1` returns to Local; `ctrl+w`
  closes a session and refuses on the Local tab; `f9` twice yields three tabs (Local + 2).
* Pilot: focus lands on the `DataTable` after a tab switch, and `tab` still switches *sides* rather
  than moving into the tab bar. This is the regression most likely to slip through.
* Pilot with the live SSH server: transfer from a remote tab to the other side, switch tabs mid
  transfer, confirm it completes; then confirm closing a tab with a live transfer is refused.
* Shutdown: open three sessions, quit, and assert every connection was closed (the server sees no
  lingering channels).

---

## Rollout

1. Part A, with its unit and integration checks. Independently shippable.
2. Part B behind the B2 contract, with the full suite green before and after.

Neither part changes `fs/`'s error boundary, the transfer engine, the vault, or the connection layer.
The one architectural claim being made is B2: the panes-per-side count becomes a detail of the widget
that owns them, and the rest of the app keeps talking about "the left pane".
