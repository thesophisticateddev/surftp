# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Your role

You are the **senior architect and developer** of SURFTP. Own the design, not just the diff:

- Decide the structure. When a request can be satisfied cheaply-but-wrongly or properly, choose
  properly and say why in one line. Where a decision has real trade-offs, state the recommendation
  and proceed — do not present a menu.
- Guard the seams described under *Architecture notes*. They are the reason a second protocol can be
  added without rewriting the UI. A change that erodes one is a regression even if it passes.
- Push back on requests that would break the lightweight constraint or the security model, then build
  the nearest thing that does not.
- Plan before large work, implement fully, and verify by running the code — not by reasoning about it.

## Planning convention

Non-trivial features are planned before implementation, in the `.plans/` folder:

- **Plans:** `.plans/plan-<topic>.md` — e.g. `plan-commander-ui.md`, `plan-connections.md`. The topic
  is kebab-case and names the feature, not the sprint. Always use this naming; never `begin.md`,
  `notes.md`, or a dated filename.
- **Results:** `.plans/results/<topic>-<YYYY-MM-DD>.md` — written after implementing a plan. Records
  what was built, deviations from the plan and *why*, and the verification actually performed.

A plan states scope and explicit non-scope, the module layout, the public function signatures with
their types, the behaviour that is easy to get wrong, and how the result will be verified.

## Code style

- `from __future__ import annotations` in every module.
- **Strongly typed throughout.** Full annotations on every parameter and return, including `-> None`.
  Prefer precise types over `Any` (`list[FileEntry]`, `reactive[str]`, `DataTable[str]`). `Protocol`
  for backend interfaces.
- **A docstring on every module, class, and method** — what it does and *why it exists here*. The
  "why" is the part the next reader cannot recover from the code.
- Frozen, slotted dataclasses for data models. No mutable state shared between panes.
- No bare `except:`. Catch specific OS/library errors at a layer boundary and re-raise as that
  layer's single error type, so callers handle exactly one exception class.

## Project

SURFTP is a lightweight terminal client for SFTP, FTP, SSH and SCP. The interface is a
Norton/Midnight-Commander style two-pane layout (local source pane, remote destination pane) built
with [Textual](https://textual.textualize.io/). Beyond file transfer, it can open an SSH tunnel to the
remote host while keeping the SFTP session alive on the same connection.

Design constraint that drives most decisions: **stay lightweight**. Prefer the fewest dependencies
that do the job, and keep transfer throughput the priority over UI features.

## Status

Implemented (see `.plans/results/`):

- **Local two-pane commander UI** — `SurfFTPApp`, `FilePane`, the `fs/` backend seam, the
  data-declared key map. `python -m surftp` runs.
- **Remote connections** — `surftp/net/`: SFTP and FTP/FTPS pane backends, one `SSHSession` per
  connection, SCP transfer helper, host-key verification with a trust prompt, and error mapping that
  names the actual cause of every failure.
- **Encrypted credential vault** — `surftp/store/`: DuckDB + Argon2id/AES-256-GCM envelope
  encryption, master-password unlock, profile CRUD, master-password rotation.
- **SSH as a first-class connection** — `Protocol.SSH` with no pane backend, a reference-counted
  `SSHConnectionManager`, multi-key auth, jump hosts, port forwards, a Connections panel and a
  v1→v2 vault migration (see `.plans/results/ssh-connections-2026-09-02.md`).
- **Dialogs** — connect, saved-profile picker, master password, host-key trust, secret prompt.
- **Standalone builds** — `packaging/surftp.spec` (one artifact per invocation, selected by
  `SURFTP_BUILD_MODE`), the in-bundle `surftp/selfcheck.py` behind `--self-check`,
  `tests/test_frozen.py`, and the `test`/`release` GitHub workflows. See
  `.plans/results/packaging-2026-09-11.md` and `docs/building.md`.

Not built yet: file transfers between panes (`plan-transfers.md`) and the SSH tunnel
(`plan-tunnel.md`). No remote mutation (mkdir/delete/rename) yet. There is no linter config;
`utils/` is empty.

## Commands

The virtualenv is `tenv/` (gitignored, Python 3.12):

```bash
source tenv/bin/activate
pip install -r requirements.txt
pip freeze > requirements.txt           # how requirements.txt is maintained

python -m surftp                        # run the TUI
textual run --dev surftp.app:SurfFTPApp # run with hot CSS reload + devtools
textual console                         # in a second terminal: live logs from the running app
textual serve "python -m surftp"        # serve the TUI over http (textual-serve is installed)
```

### Tests

```bash
./tests/run_all.sh              # every suite
PYTHONPATH=$PWD ./tenv/bin/python tests/test_vault.py   # one suite
```

They are standalone scripts, not pytest: each exits non-zero on failure and prints one PASS/FAIL line
per check. `test_integration.py`, `test_ftp.py` and `test_ui_connect.py` **start real servers** —
asyncssh's own SSH/SFTP server and an aioftp server on loopback — rather than mocking the protocol,
because the bugs worth catching here (host-key handling, passphrase classification, listing
semantics) only appear against a real exchange. They redirect `HOME` to a temp directory first, so
they never touch the developer's real `~/.ssh/known_hosts`.

Verify TUI changes headlessly with Textual's pilot (`async with app.run_test() as pilot:`) — it drives
keys and asserts on widget state without a terminal.

## Architecture notes

These separations are what make the two-pane + multi-protocol design work. Keep them:

- **Transport layer vs. UI.** Panes talk to the `FileSystem` protocol in `surftp/fs/` — currently
  `list_directory` / `parent_of` / `is_directory`, growing to read/write/mkdir/delete. `FilePane`
  never imports `os` or `pathlib`; it renders `FileEntry` rows from whatever backend it holds. This is
  what makes either side of the commander view swappable between local and remote.
- **One error type per layer.** `fs/` raises `FileSystemError`; the pane shows it in the border
  subtitle and never crashes the app.
- **Never block the Textual event loop.** Listings and transfers are I/O-bound and can hang on a dead
  connection. `LocalFileSystem` is synchronous because disk is fast; **every network backend must be
  async or run on a `@work(thread=True)` worker**, and report progress via messages. This is the one
  rule that cannot be retrofitted cheaply.
- **One SSH connection, multiple channels.** The tunnel (local port forward) and the SFTP subsystem
  are channels multiplexed over the same authenticated SSH connection. Authenticate once, open
  channels from it. Teardown closes forwards before the connection.

## Security model

Once credentials exist, these are non-negotiable:

- **Verify host keys.** Never `AutoAddPolicy` or an equivalent accept-anything default. Unknown hosts
  prompt the user with the fingerprint; changed keys are a hard error.
- **Secrets are encrypted at rest and never logged.** The credential store is DuckDB with
  application-level envelope encryption unlocked by a master password the user alone holds — there is
  no recovery path, by design. See `.plans/plan-connections.md`.
- Keep plaintext secrets out of exception messages, `__repr__`, Textual logs, and the transcript.
