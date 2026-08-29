# AGENTS.md

Guidance for OpenCode sessions working in this repo. Verify against the code before trusting prose — this repo is an early skeleton.

## Role context

You are a senior Python engineer implementing plans written as `.md` files in `.plans/`. Read the plan fully before coding; implement it as specified unless it conflicts with the constraints below.

## Plan workflow (how to follow a plan)

1. Read the `.plans/<name>.md` plan end-to-end before touching code; honor its explicit out-of-scope list.
2. Implement it as specified; deviations go only where the plan conflicts with the constraints below or with real library behavior — record every deviation and its reason.
3. Verify with an actual run (see Commands); for the TUI, headless verification via `App.run_test()` + `pilot.press(...)` works well in a plain script.
4. Write a summary to `.plans/results/<plan-name>-<YYYY-MM-DD>.md` covering: what was built, deviations/discoveries, and verification performed. Read earlier results before starting a new plan — they carry hard-won Textual gotchas.
5. Update this file's "Known broken state" section as stubs get fixed.

## Project

SURFTP: a lightweight two-pane (Norton/Midnight-Commander style) terminal client for SFTP, FTP, SSH and SCP, built with Textual. Core design constraint: **stay lightweight** — fewest dependencies, transfer throughput over UI features.

## Commands

```bash
source tenv/bin/activate            # venv is tenv/ (gitignored, Python 3.12)
pip install -r requirements.txt
pip freeze > requirements.txt       # how requirements.txt is maintained (frozen dump)

python -m surftp                    # run the TUI
textual run --dev surftp/app.py  # dev mode: hot CSS reload + devtools
textual console                     # live logs from the running app (2nd terminal)

# Testing
pytest tests/ -v                    # run all tests
pytest tests/test_permissions.py -v # run permissions tests only
pytest tests/test_session_tabs.py -v # run session tabs tests only
PYTHONPATH=/home/salman/Documents/surftp python tests/test_permissions.py  # run without pytest
PYTHONPATH=/home/salman/Documents/surftp python tests/test_shell.py        # shell unit tests (spawn-safe as a file)
```

No linter or typecheck config yet — do not invent commands; wire them up here when added.

## Known broken state (fix as you build, don't copy)

- ~~`surftp/app.py` invalid stub~~, ~~`__main__.py` imports `incus_tui`~~, ~~version split~~ — fixed by the commander-UI plan; see `.plans/results/commander-ui-2026-08-28.md`.
- ~~`requirements.txt` pins only the Textual toolchain~~ — connections plan added `asyncssh`, `aioftp`, `duckdb`, `argon2-cffi`, `cryptography`; see `.plans/results/connections-2026-08-28.md`.
- ~~`FilePane.load_directory` is synchronous~~ — connections plan moved it onto a `@work(exclusive=True)` worker.
- ~~Mode column shows only `d`/`-`~~ — pane-tabs-and-permissions plan added full POSIX permissions (`drwxr-xr-x`); see `.plans/results/pane-tabs-and-permissions-2026-08-28.md`.
- ~~Single pane per side~~ — pane-tabs-and-permissions plan added `SessionTabs` widget for multiple sessions per side.
- ~~No interactive shell~~ — ssh-shell plan added the shell channel + emulator split; see `.plans/results/ssh-shell-2026-08-28.md`.
- `docs/`, `utils/` are empty. No `pyproject.toml`, no commits on `master`.
- Transfer engine is local-only verified; SFTP/FTP transfer paths need a live server to integration-test.

## Textual 8.2.8 gotchas (learned the hard way)

- The focused `DataTable` swallows `enter` for its own `select_cursor`; app-level `enter` bindings need `priority=True`.
- Default reactives (`init=True`) fire watchers at mount **in addition to** any explicit set in `on_mount` — double loads. Use `reactive(..., init=False)` and load once explicitly.
- `DataTable.cursor_row` is read-only; use `move_cursor(row=...)`. Avoid `key=` on `add_row` unless keys are guaranteed unique — keeping your own entry list is simpler.
- Widget `__init__` must accept `*args, **kwargs` and pass them to `super().__init__()` — otherwise `id=` and other widget kwargs fail at compose time.

## Transfer engine gotchas

- **Concurrency, not cores.** The benchmark in `.plans/plan-transfers.md` §0 shows a single Python SFTP stream saturates ~3 Gbit/s on loopback. Multiple OS processes made throughput *worse*. The engine uses `asyncio.Semaphore` for a concurrency window, not a thread/process pool.
- **Partial files.** Writes go to `.surftp-partial-*` and rename on completion. An interrupted transfer never leaves a truncated file at the real path.
- **Progress throttling.** The engine flushes progress to the UI every 100 ms, not per chunk. Posting a Textual message per 128 KB chunk would starve the event loop.
- **One failure does not kill the batch.** Per-item retries (2 attempts), then mark FAILED and continue. The result names what did not transfer.

## Architecture constraints (non-negotiable)

- **Transport vs. UI separation.** Panes talk to a common filesystem-ish interface (list / stat / read / write / mkdir / delete) with one implementation per protocol (local, SFTP, FTP, SCP). Panes must not know which protocol backs them — that's what makes either side swappable.
- **Never block the Textual event loop.** Listings/transfers are I/O-bound. Use async transports or `@work(thread=True)` workers; report progress via messages.
- **One SSH connection, multiple channels.** SSH tunnel (local port forward), SFTP subsystem, and the interactive shell multiplex over a single authenticated connection. Authenticate once, open channels from it. Teardown: close channels (and shell child processes) before the connection.
- **SessionTabs contract.** Each side has a `SessionTabs` widget managing multiple `FilePane` instances. The app's `left_pane`/`right_pane` properties return the *active* tab's pane, so all existing code (actions, transfers) works unchanged. Tab 1 is always "Local" and cannot be closed.

## Session tabs gotchas

- **Focus must land on the table, never the tab bar.** `TabbedContent` inserts a focusable `Tabs` widget; if focus lands there, `enter`/arrow keys go to the wrong widget. Set the underlying `Tabs` to `can_focus = False`, and on every `TabActivated` explicitly focus the new pane's `DataTable`.
- **`active_pane` must not be fooled by the tab bar.** Ask the *side container* (`SessionTabs.has_focus_within`) and keep a `_last_active_side` fallback for the moment when focus is briefly nowhere — during a modal, for instance.
- **Never close a tab with a transfer in flight.** The engine holds a reference to the pane's `FileSystem`, not the widget, so a transfer *survives* a tab switch. But closing the tab tears down the connection under a running job. Refuse with a notification.
- **Shutdown must walk every tab.** `close_everything` iterates `left_sessions.panes + right_sessions.panes`. A missed tab is a leaked SSH connection at exit.
- **Tab count needs a ceiling.** Each session is a live SSH connection. Cap at 8 per side and say why when refusing.

## Shell gotchas

- **The emulator runs in a child process; the SSH channel stays in the parent.** The plan's measurements put terminal emulation ~300× slower than SFTP — that is the component that must never freeze the UI. `emulator.py` imports neither `asyncssh` nor `textual`; it is spawned with the `spawn` context (never `fork`, which would clone the loop + DEK into the child).
- **Read the child pipe on a worker thread, never the event loop.** `ShellSession.next_message()` blocks; call it via `@work(thread=True)` or `asyncio.to_thread`. A blocking `recv_bytes()` on the loop freezes the drain task that feeds the SSH output to the child.
- **`SSHReader.read(n)` waits until *n* bytes are available.** Keep the drain chunk small (4096), or a trickle of shell output stalls the whole channel.
- **The emulator must flush dirty frames on a pipe timeout.** If a feed arrives inside the 33 ms coalescing window, the child must poll with a timeout and emit the pending frame when due — otherwise it blocks on `recv_bytes` and the terminal looks stalled.
- **`App.check_action()` disables pane/transfer actions while a `TerminalView` is focused**, so `tab`, `ctrl+d`, `escape`, `ctrl+w`, `ctrl+pageup/pagedown` reach the shell. `f10` is the one key that always returns to the panes.
- **A `TabbedContent` subclass must not override `compose()` to declare tabs directly.** The base `compose()` builds the `ContentTabs` widget that `add_pane`/`remove_pane` and the focus handler rely on. Add every tab via `add_pane` (in `on_mount` for the first one).

See `CLAUDE.md` for the longer-form version of these notes.


## Tests

Write the test cases for all the new features added in the `test` folder. Make sure they are proper unit test cases.