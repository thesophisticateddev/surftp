# Results: SSH as a First-Class Connection (plan `plan-ssh-connections.md`)

**Date:** 2026-09-02
**Status:** Complete — all plan steps implemented and verified. One structural
deviation (bottom-panel terminal tab instead of a session-tab body) and several
library-driven deviations are recorded below.

## What was built

### `Protocol.SSH` and the connection layer (`surftp/net/`)

- **`types.py`**: `Protocol.SSH` (port 22), `ForwardKind` (local / remote /
  dynamic / socket), `ForwardSpec`, the new `ConnectionProfile` fields
  (`identity_files`, `jump_host`, `remote_command`, `keepalive_interval`,
  `compression`, `agent_forwarding`, `request_pty`, `dedicated_connection`),
  and `key_passphrase_kind(i)` for per-key passphrase records. `Credential`
  gained `key_passphrases` (one per identity file) and `passphrase_for(i)`.
  `__post_init__` flips `dedicated_connection` to `False` for SFTP/SCP/FTP so
  those keep sharing while SSH profiles keep their own connection.
- **`ssh.py`**: multi-key auth (`identity_files` → `client_keys`, in order),
  jump hosts (`tunnel=`), per-profile keepalive/compression/agent-forwarding,
  `run_command` (no PTY, returns exit status + stdout + stderr), the four
  forwards (`forward_local_port` / `forward_remote_port` / `forward_socks` /
  `forward_local_path`) as `ForwardHandle`s that name a bind failure without
  aborting the connection, and `connection_key`.
- **`manager.py`**: `SSHConnectionManager` — reference-counted
  `acquire`/`release`/`retain`/`sessions`/`close_all`, keyed by
  `(protocol, host, port, user, auth_method, identity_files)`,
  `dedicated_connection` forcing a fresh connection, jump hosts acquired
  through the same manager (shared, reference-counted, host-key-verified), and
  an 8-connection ceiling.
- **`credential_cache.py`**: the memory-only, per-session cache that prevents a
  second prompt for a dedicated SSH connection. Cleared on vault lock,
  disconnect and exit; never persisted.
- **`connect.py`**: `RemoteConnection.filesystem` is now `FileSystem | None`;
  `open_connection` gained the `Protocol.SSH` arm (no pane backend);
  `resolve_credential` resolves per-key passphrases and consults the cache.
  `RemoteConnection.close()` releases through the manager instead of closing
  directly.

### Vault migration (`surftp/store/`)

- **`schema.py`**: `SCHEMA_VERSION = 2`; the new profile columns; the
  `forwards` table + sequence; `migrate()` with the backup-first, additive,
  version-last rules. `vault.py` round-trips the new fields, added forwards
  CRUD, widened secret kinds to `SecretKind | str`, and routes `Vault.open`
  through `check_schema_version → migrate → create_schema` so the backup is a
  faithful v1 copy.

### UI (`surftp/widgets/`)

- **`connect.py`**: SSH protocol option hides the remote-path field and reveals
  identity files, a jump host, and the "open a shell on connect" toggle.
- **`connections.py`**: the Connections panel (bottom tab, `f11`) — one row per
  live session (host, auth, uptime, channels, forwards) with Shell / Command /
  Browse SFTP / Forwards / Disconnect actions.
- **`ssh_screens.py`**: the run-command view (prompt + stdout/stderr + exit
  status) and the forwards editor (start / stop / add / delete with live state).
- **`bottom.py`**: a Connections tab next to Transfers.
- **`app.py`**: `attach_ssh` opens the terminal tab and the auto-start
  forwards; panel actions run as `@work` workers; `close_everything` closes the
  manager and clears the credential cache; lock/disconnect drop cached secrets.
- **`bindings.py`**: `f10` is now `priority=True` — see deviations.

### Shell (`surftp/shell/`)

- **`session.py`**: `ShellSession.start` accepts `command` (for
  `remote_command`) and `request_pty`.

## Deviations / discoveries

1. **An SSH session's terminal lives in the bottom panel, not a session tab.**
   The plan's §6 "tab whose body is the terminal" was interpreted as the
   existing shell tab machinery, because putting a non-`FilePane` body inside
   `SessionTabs` would break the `active_pane` contract every app action routes
   through, and the plan itself says "the tab machinery does not change". The
   SSH connection is owned by the manager, its terminal tab opens in the bottom
   panel, and the Connections panel is where it is managed. "Browse this host"
   still opens a file pane lazily on the existing connection.

2. **The manager's key must include `protocol`** (deviating from the plan's §4
   list). Without it, an SFTP profile (`dedicated_connection=False`) and an SSH
   profile (`dedicated_connection=True`) to one host collide: the dedicated
   connection overwrites the shared one's registry entry, orphaning it and
   leaking it — the SFTP pane would never close its connection. The key is
   `(protocol, host, port, user, auth_method, identity_files)`; same-protocol
   profiles still share.

3. **DuckDB cannot run two `ALTER TABLE ... ADD COLUMN` on one table in a
   single transaction** ("another transaction has altered this table"), nor
   combine them into one ALTER statement. The plan's "one transaction" rule was
   therefore split: the column adds run standalone (additive and idempotent),
   and the `forwards` table + the `schema_version` bump commit together, last.
   The guarantee is preserved — a crash before that final commit leaves a vault
   that still reads as v1, and a half-migrated vault re-migrates cleanly.

4. **`f10` was a non-priority binding and could not actually escape a shell.**
   A focused `TerminalView` stops every key before the app's non-priority
   bindings run (`App._on_key` never fires once the widget handles the key).
   Making `f10` `priority=True` (like `ctrl+q`) is what makes "one key always
   returns to the panes" true — this was a latent bug from the shell plan that
   the UI pilot surfaced.

5. **`_read_shell` painted frames on a throwaway `TerminalView`.** The caller's
   `view` was never mounted; `add_shell` builds its own inside the bottom
   panel. Frames now go to `bottom_panel.view_for(tab_id)` with the caller's
   view as a fallback before the tab is mounted — otherwise the SSH terminal
   (and pane shells) would be permanently blank.

6. **The shell emulator cannot spawn under `App.run_test()`.** Textual's pilot
   gives `sys.stderr.fileno() == -1`, and CPython's `resource_tracker` passes
   that into its spawn fds → "bad value(s) in fds_to_keep". Pre-starting the
   tracker (with a real stderr) in the UI test works around it; production is
   unaffected. This is a test-harness quirk, not app code.

7. **The asyncssh test server's command bridge must call `process.exit()`** for
   an exec request, or the client never learns the exit status. And both test
   bridges must swallow `TerminalSizeChanged` (a plain `Exception`, not an
   `asyncssh.Error`), or a client-side `change_terminal_size` tears down the
   whole connection — already known from the shell plan, rediscovered here.

8. **Jump hosts authenticate with the target's credential.** The plan stores
   only `jump_host` (a string); there is no per-jump credential field, so the
   jump uses the same auth method, identity files and password/agent as the
   target. The common real-world case (agent) works; a jump with different
   credentials is out of scope for a single profile row (use `~/.ssh/config`).

9. **Socket forwards extend the `forwards` table** with two nullable columns
   (`listen_path`, `dest_path`); the plan's table shape only covered
   host/port kinds.

## Verification performed

### Unit — `tests/test_ssh_units.py` (no network)
- `Protocol.SSH` first-class (default port, `is_ssh`, `key_paths`, indexed
  passphrase kinds, `dedicated_connection` defaults).
- Profile round-trip through the vault with every new field.
- Forwards CRUD (local/remote/dynamic/socket) and per-profile isolation.
- Indexed secret kinds round-trip; legacy single row untouched.
- The credential cache: put/get, drop on disconnect, clear on lock/exit,
  temp keys for unsaved profiles, empties not cached.
- `resolve_credential` consults the cache (no second prompt) and resolves
  multiple identity-file passphrases.
- Connection key shares by address, differs by user.
- Manager sharing: SFTP shares, dedicated SSH gets its own, last release
  closes.

### Migration — `tests/test_migration.py` (highest priority)
A genuine v1 vault (v1 DDL + real ciphertexts) migrates with: every secret
still decrypting under the original master password, old profile values kept,
`.v1.bak` existing and still v1, the ciphertext byte-identical (no
re-encryption), migration working on a **locked** vault, idempotent reopen, an
interrupted migration (uncommitted ALTER) leaving a readable v1 vault that
re-migrates, a half-migrated vault completing, and a fresh v2 vault at the
current version.

### Integration — `tests/test_ssh_integration.py` (real asyncssh servers)
- `Protocol.SSH` connects with no file pane.
- `run_command` returns stdout, stderr and the exit status (0 and non-zero).
- A local port forward moves bytes end to end.
- A SOCKS proxy completes a real SOCKS5 handshake and is loopback-only.
- A jump host is refused when unknown, works once trusted, and a changed
  *target* key is caught through the tunnel (both host keys verified).
- Two identity files where the first is wrong and the second correct.
- Dedicated SSH is a separate transport while two SFTP profiles share one
  connection; disconnecting SFTP leaves the SSH session alive; SFTP-on-demand
  lists on the existing connection.

### UI (pilot) — `tests/test_ssh_ui.py`
- An SSH profile opens a terminal tab and no file pane; the session is tracked
  and the manager owns it.
- The Connections panel lists the session.
- A command run from the panel shows stdout and exit status.
- Disconnecting SFTP leaves the SSH session alive; disconnecting the SSH
  session from the panel clears the manager, the terminal tab and the tracking.

### Existing suites — all pass unmodified (plan §10)
`test_vault.py`, `test_migration.py`, `test_ssh_units.py`, `test_ui.py`,
`test_sessions.py`, `test_integration.py`, `test_ssh_integration.py`,
`test_ftp.py`, `test_transfer.py`, `test_ui_connect.py`, `test_ssh_ui.py`,
plus the pytest suites (`test_permissions`, `test_session_tabs`, `test_shell`,
`test_shell_integration`) — all green via `./tests/run_all.sh`.

## Out of scope (per plan)

X11 forwarding (noted in §5, needs a local X server a TUI cannot draw into);
rewriting the terminal emulator; `~/.ssh/config` parsing.

## What this sets up

SFTP-on-demand ("browse this host") now exists and is tested; a future plan can
surface it as a first-class action per connection. The reference-counted
manager is also the natural owner of any future connection-detail needs
(bandwidth stats, channel inspection).