# Results: SSH as a First-Class Connection — verification audit (plan `plan-ssh-connections.md`)

**Date:** 2026-09-06
**Status:** Implemented and verified, with one bug found and fixed during the audit and four plan
items still outstanding. All 13 suites pass (`./tests/run_all.sh`).

This is an audit of an implementation done in an earlier session, not a build report.

## Verified against the plan

| Plan section | State |
| --- | --- |
| §1 `Protocol.SSH`, `RemoteConnection.filesystem: FileSystem \| None` | done |
| §2.1 eight new profile fields | done, all persisted |
| §2.2 `forwards` table + CRUD | done (`list/get/save/delete_forward`) |
| §2.3 v1→v2 migration | done, with a documented deviation (below) |
| §3 ordered `identity_files`, `key_passphrase:<index>`, credential cache | done |
| §4 `SSHConnectionManager` — key, refcount, dedicated bypass, 8-connection cap | done |
| §5 shell, run-command, local/remote/dynamic/unix forwards, agent fwd, jump host, keepalive, compression, env | done except `proxy_command` |
| §6 connect-dialog SSH fields, terminal tab, connections panel, run-command screen | done |
| §7 `listen_host` defaults to `127.0.0.1`; agent forwarding defaults off | defaults correct |
| §9 migration + integration + UI verification | done, and exceeds the plan in places |
| §10 nothing broken | confirmed — all pre-existing suites pass unmodified |

Two places the implementation is **better** than the plan:

* **`connection_key` includes `protocol`**, which the plan's list omitted. The docstring explains why
  the omission would have been a bug: an SFTP profile (`dedicated_connection=False`) and an SSH
  profile (`True`) to the same host would collide in the registry, the dedicated connection would
  overwrite the shared entry, and the orphan would leak.
* **The migration test asserts the ciphertext is byte-identical** after migrating, not merely that
  secrets still decrypt — a stronger guarantee of "secrets are never touched" than §2.3 asked for.

## Accepted deviation

**§2.3's "one transaction" is partially satisfied.** DuckDB cannot run two
`ALTER TABLE ... ADD COLUMN` statements against one table inside a single transaction. The column
additions therefore run standalone (idempotent, additive, and a v1 build still reads the vault because
it selects explicit columns), while the `forwards` table and the `schema_version` bump commit together
and last. The plan's actual guarantee survives: a crash before that commit leaves a vault that still
reads as v1, and the recovery path is tested.

## Bug found and fixed during this audit

**A pane-less `Protocol.SSH` connection leaked on any teardown that was not ctrl+q.**

`SessionTabs.on_unmount` walks *panes*, and an SSH session has none — it lives only in the manager.
`get_manager().close_all()` was reachable solely from `close_everything()`, i.e. `action_quit`. Any
other exit (a headless pilot ending, a crash, SIGTERM) left the connection open and the server never
saw a disconnect. Reproduced with a probe: after the app tore down, the manager still held the session
and the test server's `wait_closed()` hung.

This is the same failure class as the leak fixed in the session-tabs work, re-introduced for the new
pane-less session type — which is exactly why §4 warned about app-level hooks.

**Fix:** `SurfFTPApp.on_unmount` now closes manager connections and clears the credential cache. An
app-level hook was wrong for panes (its children are already gone when it runs, which is what made the
original leak invisible) but is right here: the manager is a module-level registry, reachable without
the widget tree. Verified — manager holds 0 sessions afterwards and the server closes cleanly.

Guarded by a new check in `tests/test_ssh_integration.py` (`[9b]`): acquire a pane-less session, exit
`run_test` without ctrl+q, assert the manager released it and the session is closed.

## Outstanding plan items — not implemented

None is a security hole (every default is the safe one), but each is something the plan promised:

1. **`proxy_command` (§5 matrix)** — absent from the codebase entirely.
2. **Agent-forwarding UI toggle and its warning (§6, §7).** The field exists and is persisted and
   passed to asyncssh, but nothing exposes it, so it can only ever be `False`. §7 requires that the
   toggle state *"anyone with root on the remote host can use your forwarded agent to authenticate as
   you, anywhere."* Today there is no toggle and no warning.
3. **Remote-forward confirmation on first use (§7).** A `-R` forward exposes a local service to the
   remote host; the plan requires a per-profile confirmation. The forwards screen offers "Remote (-R)"
   with no warning and no confirmation.
4. **Reconnect for a dead session (§8).** The connections panel correctly renders a dropped session as
   `dead`, but offers no Reconnect action, so the tab is a dead end.

## Verification performed

`./tests/run_all.sh` — 13 suites, all passing: vault, migration, ssh-units, paths, ui, sessions,
integration, ssh-integration, ftp, transfer, ui-connect, ssh-ui, and the four pytest suites (46 tests).

The SSH integration suite covers, against real asyncssh servers on loopback: no-pane SSH connect;
command stdout/stderr/exit status; a local forward moving bytes end to end; a SOCKS proxy proven
loopback-only by a refused non-loopback connect; a jump host with *both* host keys verified (including
a changed target key through the tunnel); two identity files where only the wrong one is refused;
SFTP profiles sharing one connection while an SSH profile gets its own; a failed forward not aborting
the connection; and now the teardown regression above.
