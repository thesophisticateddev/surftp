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
```

No tests, linter, or typecheck config yet — do not invent commands; wire them up here when added.

## Known broken state (fix as you build, don't copy)

- ~~`surftp/app.py` invalid stub~~, ~~`__main__.py` imports `incus_tui`~~, ~~version split~~ — fixed by the commander-UI plan; see `.plans/results/commander-ui-2026-08-28.md`.
- `requirements.txt` pins only the Textual toolchain. **No SSH/FTP library yet** — `asyncssh` preferred (fits the async event loop); `paramiko` would need worker threads.
- `docs/`, `utils/` are empty. No `pyproject.toml`, no commits on `master`.
- `FilePane.load_directory` is synchronous — fine for `LocalFileSystem`, but a remote backend MUST move it onto a worker (flagged in `surftp/fs/local.py`).

## Textual 8.2.8 gotchas (learned the hard way)

- The focused `DataTable` swallows `enter` for its own `select_cursor`; app-level `enter` bindings need `priority=True`.
- Default reactives (`init=True`) fire watchers at mount **in addition to** any explicit set in `on_mount` — double loads. Use `reactive(..., init=False)` and load once explicitly.
- `DataTable.cursor_row` is read-only; use `move_cursor(row=...)`. Avoid `key=` on `add_row` unless keys are guaranteed unique — keeping your own entry list is simpler.

## Architecture constraints (non-negotiable)

- **Transport vs. UI separation.** Panes talk to a common filesystem-ish interface (list / stat / read / write / mkdir / delete) with one implementation per protocol (local, SFTP, FTP, SCP). Panes must not know which protocol backs them — that's what makes either side swappable.
- **Never block the Textual event loop.** Listings/transfers are I/O-bound. Use async transports or `@work(thread=True)` workers; report progress via messages.
- **One SSH connection, multiple channels.** SSH tunnel (local port forward) and SFTP subsystem multiplex over a single authenticated connection. Authenticate once, open channels from it. Teardown: close forwards before the connection.

See `CLAUDE.md` for the longer-form version of these notes.
