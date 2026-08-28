# Results: Remote Connections (plan `plan-connections.md`)

**Date:** 2026-08-28
**Status:** Complete — every plan step implemented and verified against real SSH and FTP servers.

## What was built

```
surftp/
  fs/types.py        # FileSystem protocol is now ASYNC; + FileSystemError, sort_entries
  fs/local.py        # LocalFileSystem converted to async (no awaiting; disk is fast)
  net/
    types.py         # Protocol, AuthMethod, SecretKind, ConnectionProfile, Credential, NetworkError,
                     #   HostKeyUnknown, HostKeyChanged
    errors.py        # library exception -> message that names the actual cause
    hostkeys.py      # known_hosts load/match/append, fingerprint, trust classification
    ssh.py           # SSHSession (one connection, channels on it), key loading, permission check
    sftp.py          # SFTPFileSystem — the remote pane backend
    scp.py           # ScpTransfer — copy over the session's existing connection
    ftp.py           # FTPFileSystem — FTP/FTPS via aioftp
    connect.py       # resolve_credential + open_connection (see deviation 1)
  store/
    crypto.py        # Argon2id KDF, AES-256-GCM envelope encryption, zeroize
    schema.py        # DuckDB DDL + schema-version guard
    vault.py         # Vault: create/open/unlock/lock, profile CRUD, secrets, password rotation
  widgets/
    connect.py       # ConnectDialog, ProfileListScreen
    dialogs.py       # MasterPasswordScreen, SecretPromptScreen, HostKeyScreen, MessageScreen
    pane.py          # listing moved to @work(exclusive=True); attach/detach connection
  app.py             # connect / profiles / disconnect / lock-vault flows
  bindings.py        # + f9 connect, ctrl+o profiles, ctrl+d disconnect, ctrl+l lock
  app.tcss           # one #dialog block styling every modal
tests/               # 5 standalone suites + run_all.sh
```

Dependencies added: `asyncssh`, `aioftp`, `duckdb`, `cryptography`, `argon2-cffi` — plus **`bcrypt`**,
which the plan did not list (see deviation 2).

## Deviations from the plan, and why

1. **`resolve_credential` lives in `net/connect.py`, not `net/ssh.py`.** The plan put the resolution
   order in `ssh.py` "so the rules live in exactly one place". They do — but placing them in
   `connect.py` alongside the protocol dispatch keeps `ssh.py` from importing the FTP client and keeps
   the vault dependency out of the SSH transport. Same single-place property, cleaner dependency graph.
2. **`bcrypt` is a required dependency, not optional.** Without it asyncssh cannot decrypt
   passphrase-protected keys in the *OpenSSH* format — which is what `ssh-keygen` produces by default.
   The plan's key-auth requirement is unmet without it. Caught by the integration test failing to even
   generate such a key.
3. **`FileSystem` gained a `label` property** (a fourth member, where the plan said three). The pane
   title has to show `user@host` for a remote pane and a bare path for a local one; without a label on
   the backend the pane would have to ask "which protocol am I?", which is exactly the knowledge the
   seam exists to deny it.
4. **`FilePane.go_parent` needed a second worker.** `parent_of` is async, so the synchronous action
   cannot await it. It is non-exclusive, so it does not cancel the listing worker it then triggers.
5. **No `pytest`.** The suites are standalone scripts, each starting its own server. Wiring pytest +
   pytest-asyncio was not in scope and would have added two dependencies for no coverage gain.

## Bugs found and fixed during verification

1. **First-run crash on a machine that has never used SSH.** With no `~/.ssh/known_hosts`, asyncssh
   raised `FileNotFoundError`, surfaced as "Cannot reach host: No such file or directory" — instead of
   the trust prompt. `known_hosts_path()` now creates the file empty. This is the *first* thing a new
   user would have hit.
2. **Passphrase failures misreported as malformed keys.** asyncssh raises `KeyEncryptionError` for
   OpenSSH-format keys but a plain `KeyImportError` for PKCS#8, with the reason only in the message.
   A wrong passphrase on a `.pem` therefore produced "Could not read <file>" — sending the user to
   check the file rather than their typing, the exact confusion the plan set out to prevent.
   `_is_passphrase_failure()` now classifies both, in one place.

## Verification performed

`./tests/run_all.sh` — all 5 suites, ~85 checks, passing.

- **`test_vault.py`** (no network): DEK round trip; wrong password rejected; tampered ciphertext
  fails its tag; AAD blocks ciphertext reuse across both profile id and secret kind; unique nonces;
  zeroize; stored KDF params honoured; locked vault refuses reads *and* writes while still listing
  profiles; rotation preserves every secret, invalidates the old password, and survives reopen;
  profile CRUD including "rename does not orphan secrets" and "delete cascades to secrets".
- **`test_integration.py`** (real asyncssh SSH/SFTP server on loopback): unknown host raises with a
  SHA-256 fingerprint; listing order, sizes and mtimes; password auth; wrong password named as such;
  `.pem` auth across four key shapes (PKCS#1 RSA, PKCS#8 encrypted, OpenSSH encrypted, malformed)
  producing four distinct messages; vault-stored key and password; locked vault falling through to a
  prompt; SCP profile browsing over SFTP; changed host key as a hard failure; closed port and bad DNS
  as distinct messages.
- **`test_ftp.py`** (real aioftp server): MLSD listing with sizes and mtimes, sort order, parent link
  below root only, rejected login and closed port messages.
- **`test_ui.py`** (Textual pilot): listing, tab focus, enter/backspace, hidden toggle, `/root` error
  in the border subtitle, FTP clear-text warning, port following the protocol selector, vault-creation
  prompt carrying the "NO recovery" warning.
- **`test_ui_connect.py`** (pilot against a live server): host-key prompt with Reject focused by
  default, trust-and-connect, remote listing rendered in the pane, the other pane staying local,
  `tab` still switching panes while remote, remote navigation and refresh, `ctrl+d` restoring the
  previous local path, wrong password landing in the pane subtitle, save-to-vault demanding a master
  password first, and reconnecting from the saved profile with nothing typed. Also asserts the stored
  password is not present as plaintext in the DuckDB blob.

## Security properties as built

- Host keys verified against `~/.ssh/known_hosts` on every connect; `known_hosts=None` is never
  passed. The one unverified exchange is `get_server_host_key`, which stops before authentication and
  sends no credentials — it exists only to render a fingerprint. Changed keys have no UI override.
- Envelope encryption: Argon2id (t=3, m=64MiB, p=4) -> KEK -> wraps a random DEK -> AES-256-GCM per
  secret with a fresh nonce and the profile id + kind as associated data. No verifier record.
- The vault file is created `0600`; `known_hosts` is chmod'd `0600` after append.
- `Credential.__repr__` is redacted so a traceback cannot leak a secret.
- "Save to vault" is unchecked by default; plain FTP shows a clear-text warning.

## Out of scope (unchanged)

Transfers, remote mutation, and the tunnel. The tunnel's prerequisite is met: `SSHSession` exposes its
connection, and `RemoteConnection.close()` already tears the channel down before the connection.
