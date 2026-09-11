# Plan: SSH as a First-Class Connection

**Goal:** make SSH a protocol SURFTP connects with in its own right — a dedicated connection per SSH
profile, its settings persisted in the vault, `.pem` identity files, and the full range of what an SSH
connection can do: shell, command execution, local/remote/dynamic port forwarding, agent forwarding
and jump hosts.

**In scope:** `Protocol.SSH`; a connection manager owning dedicated connections; profile fields and
the vault migration that stores them; multiple identity files; the forwarding subsystem; a
connection-detail UI.

**Out of scope:** X11 forwarding (asyncssh supports it, but it needs a local X server and a display
channel that a terminal UI cannot usefully present — noted in §5 rather than silently dropped);
rewriting the terminal emulator (already built, see `plan-ssh-shell.md`); and SSH config file
(`~/.ssh/config`) parsing, which deserves its own plan because its matching rules are a feature in
themselves.

---

## 0. What a separate connection actually buys — and what it costs

Recorded plainly, because it was measured while planning the shell and the numbers should not be
quietly forgotten:

| | SFTP throughput |
| --- | --- |
| SFTP alone | 371 MB/s |
| SFTP + busy shell, **same** connection | 159 MB/s |
| SFTP + busy shell, **separate** connection | 172 MB/s |

**A separate connection is not a performance win.** The contention is the Python process, not the SSH
connection, so this plan should not be justified on isolation grounds and the implementation should
not promise it. What a dedicated connection genuinely provides:

* **Independent lifecycle.** An SSH session that survives an SFTP failure and vice versa. Today a
  dropped SFTP subsystem takes the shell with it, because the shell is a channel on that connection.
* **SSH without a file pane.** A profile whose purpose is a terminal or a tunnel, with no file
  browsing at all — impossible today, since every profile is a pane backend.
* **Independent settings.** Forwards, agent forwarding and a remote command belong to an SSH session,
  not to a file browser.

And the cost, which the design must handle rather than ignore: **a second connection means a second
authentication.** §3 covers how that happens without prompting the user twice.

---

## 1. `Protocol.SSH`

`Protocol` gains `SSH = "ssh"` (default port 22). It differs from `SFTP`/`SCP` in one structural way:
**an SSH profile has no pane backend.** `open_connection` currently returns a `RemoteConnection` with
a `filesystem`; for SSH that field is `None`, and the session opens a terminal tab rather than
occupying a file pane.

This is the one place the existing seam bends, so it is worth being explicit: `RemoteConnection.filesystem`
becomes `FileSystem | None`, and the two call sites that attach it to a pane must refuse an SSH
profile with a message rather than crashing on `None`. Everything else — host-key verification,
credential resolution, the vault — is shared unchanged.

An SSH profile may still open SFTP on demand ("browse this host"), which creates the file pane
lazily on the connection that already exists. That is the reverse of today's flow and is the payoff
of separating the two.

---

## 2. Profile model and the vault migration

### 2.1 New profile fields

```python
@dataclass(frozen=True, slots=True)
class ConnectionProfile:
    ...                                   # existing fields unchanged
    identity_files: tuple[str, ...] = ()  # ordered, like repeated ssh -i
    jump_host: str | None = None          # ProxyJump, "user@host:port"
    remote_command: str | None = None     # run instead of a login shell
    keepalive_interval: int = 30
    compression: bool = False
    agent_forwarding: bool = False        # off by default; see §7
    request_pty: bool = True
    dedicated_connection: bool = True     # SSH profiles default to their own
```

`pem_path` stays, as the single-key case and for backward compatibility; `identity_files` is the
general form and `pem_path` is treated as its first element when set. Keeping both avoids rewriting
every existing profile at migration time.

### 2.2 Port forwards: a separate table

A profile can carry many forwards, so they are rows, not a JSON blob — queryable, and editable one at
a time:

```sql
CREATE TABLE forwards (
    id          UBIGINT PRIMARY KEY DEFAULT nextval('forward_ids'),
    profile_id  UBIGINT NOT NULL,
    kind        VARCHAR NOT NULL,   -- 'local' | 'remote' | 'dynamic'
    listen_host VARCHAR NOT NULL,   -- default '127.0.0.1'; see §7
    listen_port INTEGER NOT NULL,
    dest_host   VARCHAR,            -- NULL for 'dynamic'
    dest_port   INTEGER,            -- NULL for 'dynamic'
    auto_start  BOOLEAN NOT NULL DEFAULT TRUE
);
```

### 2.3 The migration — the riskiest part of this plan

`SCHEMA_VERSION` is 1 today and `check_schema_version` only *refuses* a newer vault; there is no
migration path at all. This plan is the first change to the schema, so it has to build one, and it is
operating on a store whose contents are **unrecoverable if damaged** — there is no backup of a master
password.

Rules:

1. **Copy the vault file to `vault.duckdb.v1.bak` before touching it**, and say so in the UI. A
   failed migration must be recoverable by moving one file back.
2. **Additive only.** `ALTER TABLE ... ADD COLUMN` with defaults, plus the new `forwards` table. Never
   drop or rename a column; a v1 build should still read what it understands.
3. **One transaction**, with `vault_meta.schema_version` written *last*. A crash mid-migration then
   leaves a v1 vault, not a half-v2 one.
4. **Do not touch `secrets`.** The ciphertexts and their associated data (profile id + kind) are
   unchanged, so no re-encryption and no unlock is required to migrate. Migration must work on a
   **locked** vault — requiring the master password to upgrade would be a trap for a user who just
   wanted to open the app.
5. `check_schema_version` keeps refusing a *newer* vault, unchanged.

Verification for this section is not optional: migrate a v1 vault containing profiles and secrets,
then assert every secret still decrypts with the original master password.

---

## 3. Identity files and authentication

`.pem` support already exists (`AuthMethod.PEM_FILE`, `load_private_key`, passphrase classification,
the group/world-readable warning). This plan generalises it:

* **Multiple identities, in order.** `identity_files` maps to asyncssh's `client_keys`, which accepts
  a list and tries them in order — the same semantics as repeated `ssh -i`.
* **Per-key passphrases** are stored in the vault as `SecretKind.KEY_PASSPHRASE`. With several keys,
  the kind becomes `key_passphrase:<index>` so keys do not share one passphrase record. This is a new
  secret kind pattern; the associated-data binding (`profile_id:kind`) already makes it safe.
* **Permission check per file**, reusing `check_key_permissions`, warning rather than refusing.
* **Agent** stays available as its own `AuthMethod`, and is also the fallback when no identity file
  loads.

**Avoiding a second password prompt.** A dedicated connection authenticates separately, so:

1. If the profile has a vault-stored secret, `resolve_credential` supplies it — no prompt. This is
   the normal path and requires no new code.
2. If the user typed a one-off secret for an earlier connection to the *same profile*, it is held in
   a **memory-only, per-session credential cache** keyed by profile id, cleared on vault lock, on
   profile disconnect, and at exit. It is never written to disk — that is the whole distinction from
   "Save to vault".
3. Otherwise, prompt once, and offer "use for this session".

The cache is the only new place a plaintext secret lives, so it gets an explicit lifetime and an
explicit owner (§7).

---

## 4. The connection manager

```python
class SSHConnectionManager:
    """Owns every live SSH connection, and decides when to share one."""

    async def acquire(self, profile: ConnectionProfile, credential: Credential) -> SSHSession: ...
    async def release(self, session: SSHSession) -> None: ...
    def sessions(self) -> list[SSHSession]: ...
    async def close_all(self) -> None: ...
```

* **Keyed by `(host, port, username, auth_method, identity_files)`** — never by profile name, or two
  profiles pointing at one server would fail to share when sharing was wanted.
* **`dedicated_connection` forces a new connection** even when a matching one exists. SSH profiles
  default to `True`, which is the behaviour asked for here; SFTP profiles default to `False` so
  today's "one connection, many channels" behaviour is preserved exactly.
* **Reference counted.** A connection closes when its last consumer releases it — a shell tab, an
  SFTP pane and three forwards can share one session, and the connection must outlive the first of
  them to close. Getting this wrong yields either leaked connections or a tunnel that dies when a
  pane closes.
* **Registered for shutdown.** `close_all` runs from the widget that owns the sessions, not from an
  app-level hook — the leaked-connection bug in the tabs work proved that `App.on_unmount` fires after
  its children are gone.

---

## 5. Full spectrum: the feature matrix

Every capability below is native to asyncssh (verified against 2.24 while writing this), so none of it
requires reimplementing protocol machinery.

| Capability | `ssh(1)` | asyncssh | Notes |
| --- | --- | --- | --- |
| Interactive shell | *(default)* | `create_process(term_type=...)` | Already built; reuse `surftp/shell/` |
| Run a command | `ssh host cmd` | `conn.run(cmd)` / `create_process(command=)` | Output to a result pane, exit status shown |
| Local forward | `-L` | `forward_local_port(...)` | Returns a listener to hold and close |
| Remote forward | `-R` | `forward_remote_port(...)` | Server must permit it; failure is common and must be reported clearly |
| Dynamic SOCKS proxy | `-D` | `forward_socks(...)` | Bind to loopback only (§7) |
| Unix-socket forward | `-L` w/ socket | `forward_local_path(...)` | Useful for a remote Docker socket |
| Agent forwarding | `-A` | `agent_forwarding=True` | Off by default (§7) |
| Jump host | `-J` | `tunnel=<connection>` | Open the jump connection first, pass it as `tunnel` |
| Proxy command | `ProxyCommand` | `proxy_command=` | For exotic transports |
| Keepalive | `ServerAliveInterval` | `keepalive_interval=` | Already set to 30 s |
| Compression | `-C` | `compression_algs=` | Helps on slow links, costs CPU on fast ones |
| Environment | `SendEnv` | `env=` on the process | Servers usually restrict what they accept |
| SFTP / SCP | *(subsystem)* | `start_sftp_client()`, `scp()` | Already built |
| X11 forwarding | `-X` | `x11_forwarding=True` | **Out of scope**: needs a local X server; a TUI has nothing to draw it into |

**Jump hosts are connections too.** A jump host must be acquired through the same manager (so it can
be shared and reference-counted) and its own host key must be verified — a `ProxyJump` that skipped
verification would be a hole straight through the security model.

**Forwards have lifecycle.** Each is a listener object; starting one can fail (port in use locally,
server refusing a remote bind) and that failure must name which forward and why. Forwards with
`auto_start` come up after authentication; the rest are started from the UI.

---

## 6. UI

* **Connect dialog** gains an SSH protocol option. Choosing it hides the remote-path field and reveals
  identity files, jump host, and an "open a shell on connect" toggle.
* **Session tabs** already host per-session content. An SSH profile opens a tab whose body is the
  terminal rather than a file pane — the tab machinery does not change.
* **A connection panel** (in the bottom tabbed panel, beside Transfers and Shell) lists live
  connections: host, auth method, uptime, channel count, and the forwards with their state. Forwards
  can be started, stopped and added there.
* **Run-command** view: a prompt, the command's stdout/stderr, and its exit status. Distinct from the
  shell because a non-interactive command needs no PTY and its output is worth keeping.

---

## 7. Security

* **Agent forwarding is off by default and warns when enabled.** Anyone with root on the remote host
  can use your forwarded agent to authenticate as you, anywhere. The toggle must say that.
* **Listeners bind to `127.0.0.1` by default.** A dynamic SOCKS proxy or a local forward bound to
  `0.0.0.0` turns the user's machine into an open relay into the remote network. Binding elsewhere is
  possible but requires typing the address, never a default.
* **Remote forwards expose a local service to the remote host.** Confirm on first use per profile.
* **Jump hosts get full host-key verification**, like any other connection.
* **The in-memory credential cache** (§3) holds plaintext for the session only: cleared on vault lock,
  on disconnect, and at exit; never logged, never in a `__repr__` (`Credential` is already redacted),
  never persisted. It is opt-in per connection.
* Identity files are read from disk and never copied into the vault unless the user chooses
  `PEM_STORED`, exactly as today.

---

## 8. Failure and lifecycle

* Connection lost → mark the session dead, keep the tab with its scrollback, offer Reconnect. Forwards
  on that connection are marked stopped, not silently forgotten.
* A forward that fails to bind reports the port and the reason and does not abort the connection.
* Closing an SSH tab releases one reference; the connection closes only when nothing else holds it.
* Cap concurrent connections (reuse the existing 8-session ceiling) so a key-repeat cannot open forty.

---

## 9. Verification

**Unit:** the profile round-trips through the vault with every new field; forwards CRUD; the
credential cache clears on lock, disconnect and exit.

**Migration (highest priority):** build a v1 vault with profiles and secrets, migrate it, then assert
(a) every secret still decrypts with the original master password, (b) old profiles keep their values,
(c) the `.v1.bak` file exists, (d) migrating a locked vault works, and (e) an interrupted migration
leaves a readable v1 vault.

**Integration, against the asyncssh test server:** connect with `Protocol.SSH` and no file pane; run a
command and check stdout and exit status; open a local forward and move bytes through it end to end;
open a SOCKS proxy and prove it binds to loopback only; connect through a jump host (two servers on
loopback) and confirm *both* host keys were verified; authenticate with two identity files where the
first is wrong and the second correct; confirm a dedicated connection is a genuinely separate
transport while an SFTP profile still shares.

**UI (pilot):** an SSH profile opens a terminal tab and no file pane; the connection panel lists the
session and its forwards; disconnecting SFTP leaves the SSH session alive — the independence this
plan exists for.

---

## 10. What must not break

All seven suites pass unmodified. Specifically: `Protocol.SFTP` profiles keep sharing one connection
(`dedicated_connection=False`), so the "one connection, many channels" property and the shell that
depends on it are unchanged; existing v1 vaults open and every stored secret still decrypts; the
`FileSystem` seam, transfer engine and pane APIs are untouched. The only widened type is
`RemoteConnection.filesystem`, and both call sites that consume it must be updated together with it.
