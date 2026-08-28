# Plan: Remote Connections (SFTP / SSH / SCP / FTP) with an Encrypted Credential Store

**Goal:** the right pane can connect to a remote host over SFTP, SCP or FTP and browse it with the
existing key map, authenticating with either a `.pem` private key or a username/password. Credentials
are saved locally in a DuckDB vault, encrypted, unlocked by a master password that only the user
holds.

**In scope**
- A connection/session layer (`surftp/net/`) with one async backend per protocol, each satisfying the
  existing `FileSystem` protocol so `FilePane` is untouched.
- Auth: PEM private key (with or without passphrase) and username/password. Agent auth falls out of
  asyncssh for free and is worth wiring while we are here.
- Host-key verification with an interactive trust prompt.
- An encrypted credential vault (`surftp/store/`) over DuckDB, with master-password unlock.
- A connect dialog + profile picker, and the bindings to reach them.

**Out of scope (separate plans)**
- File transfers between panes — `plan-transfers.md`.
- The SSH tunnel / port forwarding — `plan-tunnel.md`. This plan only guarantees the connection object
  is reusable for it (one connection, many channels).
- Any remote mutation (mkdir/delete/rename). Read-only browsing here.

---

## 1. Dependencies to add

| Package | Why this one |
| --- | --- |
| `asyncssh` | asyncio-native SSH, SFTP **and** SCP and port-forwarding in one library. Matches Textual's event loop with no worker threads, and gives us the "one connection, many channels" property the tunnel needs. `paramiko` would force a thread pool and a second library for forwarding. |
| `aioftp` | async FTP/FTPS client. Python's stdlib `ftplib` is synchronous and would need threading. |
| `duckdb` | the vault store (specified). |
| `cryptography` | AES-256-GCM for the envelope encryption. Already an `asyncssh` dependency, so it costs nothing extra. |
| `argon2-cffi` | Argon2id KDF for the master password. |

Pin them and refresh `requirements.txt` via `pip freeze`. Five additions is the floor for this feature
set; note in the commit why each is unavoidable so the lightweight constraint stays auditable.

---

## 2. Module layout

```
surftp/
  net/
    __init__.py
    types.py        # ConnectionProfile, AuthMethod, Credential, NetworkError
    ssh.py          # SSHSession: the one authenticated connection
    sftp.py         # SFTPFileSystem  — FileSystem over an SSHSession channel
    scp.py          # ScpTransfer     — file copy over the same SSHSession
    ftp.py          # FTPFileSystem   — FileSystem over aioftp
    hostkeys.py     # known_hosts loading + the trust decision
  store/
    __init__.py
    vault.py        # Vault: open/unlock/CRUD over DuckDB
    crypto.py       # KDF + envelope encrypt/decrypt. No DuckDB imports here.
    schema.py       # DDL + schema_version migration
  widgets/
    connect.py      # ConnectDialog, ProfileList, MasterPasswordPrompt (ModalScreens)
```

`store/crypto.py` must not import `duckdb`, and `store/vault.py` must not implement crypto. Keeping
them apart is what makes the crypto unit-testable without a database and reviewable on its own.

---

## 3. The credential vault

### 3.1 Key hierarchy (envelope encryption)

```
master password ──Argon2id(salt, t=3, m=64MiB, p=4)──▶ KEK (32B, never stored)
                                                        │
                          wrapped_dek = AES-256-GCM(KEK, DEK) ── stored in vault_meta
                                                        │
                                        DEK (32B, random, in memory only while unlocked)
                                                        │
                              secret ciphertext = AES-256-GCM(DEK, plaintext, nonce)
```

Why two levels rather than encrypting each secret with the password-derived key directly: changing the
master password then re-wraps **one** 32-byte DEK instead of decrypting and re-encrypting every stored
secret. That single property is worth the extra indirection.

**No separate password verifier.** AES-GCM is authenticated: if unwrapping the DEK fails its tag
check, the password was wrong. Storing a verifier hash would only hand an attacker a second oracle.

**There is no recovery path.** A lost master password means a lost vault, by design — this must be
stated in the UI at vault-creation time, not buried in docs.

### 3.2 `store/crypto.py`

```python
KDF_TIME_COST: int = 3
KDF_MEMORY_COST_KIB: int = 65536
KDF_PARALLELISM: int = 4
DEK_SIZE_BYTES: int = 32
NONCE_SIZE_BYTES: int = 12

def derive_kek(master_password: str, salt: bytes) -> bytes: ...
def generate_dek() -> bytes: ...
def wrap_dek(kek: bytes, dek: bytes) -> bytes: ...
def unwrap_dek(kek: bytes, wrapped: bytes) -> bytes: ...   # raises VaultLockedError on bad password
def encrypt_secret(dek: bytes, plaintext: str) -> bytes: ...
def decrypt_secret(dek: bytes, ciphertext: bytes) -> str: ...
```

- Every ciphertext is stored as `nonce || ct || tag` in one `BLOB`. A **fresh random nonce per
  encryption** — reusing a nonce under one key breaks GCM catastrophically. Never derive the nonce
  from the record id or a counter.
- Bind each secret to its row with GCM **associated data** (`f"{profile_id}:{secret_kind}"`), so a
  ciphertext copied from one profile row to another fails to decrypt instead of silently
  authenticating the wrong host's password.
- `zeroize(buf: bytearray) -> None` best-effort wipe on lock. Note honestly in its docstring that
  Python cannot guarantee this for `str`/`bytes`; it applies to the `bytearray`-held DEK only.

### 3.3 DuckDB schema (`store/schema.py`)

```sql
CREATE TABLE vault_meta (           -- exactly one row
    schema_version  INTEGER NOT NULL,
    kdf_salt        BLOB    NOT NULL,
    kdf_params      JSON    NOT NULL,   -- so cost params can be raised later
    wrapped_dek     BLOB    NOT NULL,
    created_at      TIMESTAMP NOT NULL
);

CREATE TABLE profiles (
    id            UBIGINT PRIMARY KEY,
    name          VARCHAR NOT NULL UNIQUE,   -- shown in the picker
    protocol      VARCHAR NOT NULL,          -- 'sftp' | 'scp' | 'ftp'
    host          VARCHAR NOT NULL,
    port          INTEGER NOT NULL,
    username      VARCHAR NOT NULL,
    auth_method   VARCHAR NOT NULL,          -- 'password' | 'pem_file' | 'pem_stored' | 'agent'
    pem_path      VARCHAR,                   -- set when auth_method = 'pem_file'
    remote_path   VARCHAR,                   -- initial directory, optional
    use_tls       BOOLEAN NOT NULL DEFAULT FALSE,  -- FTPS
    created_at    TIMESTAMP NOT NULL,
    last_used_at  TIMESTAMP
);

CREATE TABLE secrets (
    profile_id  UBIGINT NOT NULL REFERENCES profiles(id),
    kind        VARCHAR NOT NULL,   -- 'password' | 'key_passphrase' | 'private_key'
    ciphertext  BLOB    NOT NULL,   -- nonce || ct || tag
    PRIMARY KEY (profile_id, kind)
);
```

Only the `secrets` table holds encrypted data — hostnames and usernames stay queryable so the picker
can render without an unlock. That is a deliberate trade: the vault protects secrets, not the fact
that you have an account on a host. Say so in the module docstring.

Vault location: `platformdirs.user_data_dir("surftp")/vault.duckdb` (`platformdirs` is already
installed). Create the file `0600`.

DuckDB 1.4+ can also encrypt the database file itself (`ATTACH ... (ENCRYPTION_KEY ...)`). Treat that
as optional defence-in-depth layered *on top* — application-level envelope encryption stays the source
of truth, so the security model does not depend on the pinned DuckDB version.

### 3.4 `store/vault.py`

```python
class Vault:
    """DuckDB-backed credential store. Locked until unlock() succeeds."""

    @classmethod
    def create(cls, path: Path, master_password: str) -> Vault: ...
    @classmethod
    def open(cls, path: Path) -> Vault: ...          # locked; profiles still listable

    def unlock(self, master_password: str) -> None: ...   # raises VaultLockedError
    def lock(self) -> None: ...                            # zeroizes the DEK
    @property
    def is_unlocked(self) -> bool: ...

    def list_profiles(self) -> list[ConnectionProfile]: ...          # no unlock needed
    def get_profile(self, profile_id: int) -> ConnectionProfile: ...
    def save_profile(self, profile: ConnectionProfile) -> int: ...
    def delete_profile(self, profile_id: int) -> None: ...           # cascades secrets

    def put_secret(self, profile_id: int, kind: str, value: str) -> None: ...
    def get_secret(self, profile_id: int, kind: str) -> str | None: ...  # requires unlock
    def change_master_password(self, old: str, new: str) -> None: ...    # re-wraps the DEK only
```

Every secret-touching method raises `VaultLockedError` when locked — check the flag, never return
`None` for "locked", or a caller will read it as "no password set" and fall through to a prompt.

---

## 4. Authentication

`net/types.py`:

```python
class AuthMethod(StrEnum):
    PASSWORD = "password"        # username + password
    PEM_FILE = "pem_file"        # key stays on disk; vault holds only the path (+ passphrase)
    PEM_STORED = "pem_stored"    # key material imported into the vault, encrypted
    AGENT = "agent"              # SSH_AUTH_SOCK

@dataclass(frozen=True, slots=True)
class ConnectionProfile:
    """A saved target. Contains no secrets — those live in the vault, keyed by id."""
    id: int | None
    name: str
    protocol: Protocol_          # SFTP | SCP | FTP
    host: str
    port: int
    username: str
    auth_method: AuthMethod
    pem_path: str | None = None
    remote_path: str | None = None
    use_tls: bool = False
```

**PEM handling**
- `PEM_FILE` is the default and the recommended choice: SURFTP stores the *path*, and the key never
  enters the database. `PEM_STORED` exists for users who want one portable vault file; it is opt-in
  and the dialog must say the key material is being copied into the vault.
- Check permissions on load: warn if the file is group/world-readable (mode `& 0o077`), matching
  OpenSSH's behaviour. Warn, do not refuse.
- Encrypted PEMs: attempt load without a passphrase, and on failure prompt (or read the stored
  `key_passphrase` secret). Passphrase-protected keys must be supported — they are the common case for
  a key that is worth protecting.
- Load via `asyncssh.read_private_key(path, passphrase=...)`; it accepts PEM (PKCS#1/PKCS#8), OpenSSH
  and PuTTY formats, so `.pem` from AWS works unchanged. Map its `KeyImportError` to a message naming
  the actual cause (wrong passphrase vs. malformed file) — "auth failed" for a typo'd passphrase is
  the single most confusing failure in a client like this.

**Password auth**: `asyncssh.connect(..., password=...)`. For FTP, `aioftp.Client.connect` +
`login(user, password)`.

**Resolution order** in `net/ssh.py`, so the rules live in exactly one place:
1. explicit credential passed by the dialog (a one-off connection, never persisted unless asked),
2. vault secret for the profile,
3. interactive prompt,
4. agent, if `AuthMethod.AGENT`.

---

## 5. Host-key verification (`net/hostkeys.py`)

```python
def load_known_hosts() -> Path: ...
def verify_or_prompt(host: str, port: int, key: asyncssh.SSHKey) -> HostKeyDecision: ...
def remember_host_key(host: str, port: int, key: asyncssh.SSHKey) -> None: ...
```

- Default to `~/.ssh/known_hosts` so SURFTP inherits the trust the user already has.
- **Unknown host:** show the SHA-256 fingerprint and key type in a modal; the user accepts (append to
  known_hosts) or cancels. Accepting is an explicit keystroke, never a default-highlighted button.
- **Changed key:** hard failure with the mismatch shown. No "accept anyway" path in the UI.
- Never pass `known_hosts=None` to `asyncssh.connect` — that disables verification entirely. If a
  debug escape hatch is ever needed it is an env var, off by default, and it must log loudly.

---

## 6. Connections and backends

### `net/ssh.py`

```python
class SSHSession:
    """One authenticated SSH connection. SFTP, SCP and (later) tunnels are channels on it."""

    @classmethod
    async def connect(cls, profile: ConnectionProfile, credential: Credential) -> SSHSession: ...
    async def start_sftp(self) -> SFTPFileSystem: ...
    async def close(self) -> None: ...
    @property
    def is_connected(self) -> bool: ...
```

Authenticate once; `start_sftp()` opens a channel on the existing connection. Set `keepalive_interval`
so a half-dead connection surfaces as an error instead of hanging a pane forever.

### `net/sftp.py` — `SFTPFileSystem`

Implements the same three methods as `LocalFileSystem`, but **async**:
`async def list_directory(self, path: str) -> list[FileEntry]`, `parent_of`, `is_directory`.

Two consequences to handle deliberately:

1. **The `FileSystem` protocol becomes async.** Cleanest is to make the protocol async and have
   `LocalFileSystem`'s methods `async def` returning immediately — one code path in the pane, no
   `inspect.iscoroutine` branching. This is a small, contained edit to `fs/local.py` and `pane.py`.
2. **`FilePane.load_directory` must move onto a worker** (`@work(exclusive=True)`), showing a loading
   state and leaving the UI responsive. `exclusive=True` cancels a superseded listing when the user
   navigates fast. This is the rule flagged in `fs/local.py` since day one — this plan is where it
   comes due.

Map `asyncssh.SFTPError` / `aioftp` errors to `FileSystemError` at the backend boundary so the pane's
existing error handling works unchanged. Reuse `stat` results from `readdir` — do not re-`stat` each
entry over the network, or a 1000-file directory becomes 1000 round trips.

### `net/scp.py`
SCP is a transfer protocol, not a browsable filesystem: there is no portable remote listing. So an
SCP profile browses over **SFTP** and transfers via `asyncssh.scp` on the same `SSHSession`. Where the
server has SFTP disabled, the pane shows that explicitly rather than an empty listing. Document this,
because "SCP" in the picker implying a browsable pane is the obvious wrong assumption.

### `net/ftp.py` — `FTPFileSystem`
`aioftp.Client`, passive mode, `use_tls` selecting FTPS. Parse `MLSD` where available (structured,
reliable) and fall back to `LIST` parsing otherwise. Plain FTP sends credentials in clear text — the
dialog must show a visible warning when `use_tls` is off, and vault-stored FTP passwords deserve the
same warning at save time.

---

## 7. UI

- `MasterPasswordPrompt` — a `ModalScreen[str]`, `password=True` on the `Input`. On first run it
  offers vault creation with the "no recovery" warning and a confirm field.
- `ConnectDialog` — a `ModalScreen[ConnectionProfile | None]`: protocol, host, port (defaulted per
  protocol), username, auth method, and a conditional area (password field / PEM path with a browse
  action / passphrase). A "Save to vault" checkbox, unchecked by default — a one-off connection must
  not silently persist a password.
- `ProfileList` — saved profiles ordered by `last_used_at`, `enter` connects, `delete` removes.
- Connection state lives on the pane: title shows `user@host:/path` when connected, and disconnect
  reverts the pane to `LocalFileSystem` at the last local path.

New bindings in `bindings.py` (the existing file already reserves F2–F8 for commander operations, so
these must not collide):

| Key | Action | Description |
| --- | --- | --- |
| `f9` | `connect` | Connect the focused pane |
| `ctrl+o` | `open_profiles` | Saved profiles |
| `ctrl+d` | `disconnect` | Disconnect the focused pane |
| `ctrl+l` | `lock_vault` | Lock the credential vault |

---

## 8. Failure behaviour

Wrong password, wrong passphrase, unknown host, changed host key, DNS failure, refused connection,
timeout, and vault-locked are **each distinct messages**. A single "connection failed" is the failure
mode that makes a client like this unusable, and every one of these is a normal Tuesday. Connection
errors surface in the pane's border subtitle exactly like `FileSystemError` does today.

---

## 9. Verification

**Unit (no network, no terminal)**
- Crypto: encrypt/decrypt round trip; wrong master password raises `VaultLockedError`; a tampered
  ciphertext byte fails the GCM tag; associated data prevents cross-profile ciphertext reuse; nonces
  differ across two encryptions of identical plaintext.
- Vault: create → save profile + secret → lock → `get_secret` raises → unlock → matches;
  `change_master_password` preserves every secret and invalidates the old password; profiles list
  while locked.
- PEM: load an unencrypted key, a passphrase-protected key with the right and wrong passphrase, and a
  malformed file — asserting three *different* error messages.

**Integration (local server)**
- `sshd` on localhost, or `docker run -p 2222:22 lscr.io/linuxserver/openssh-server`: connect with a
  generated `.pem`, then with a password; list a directory; verify the trust prompt appears on first
  connect and not on the second; flip the host key and confirm a hard failure.
- FTP against a local `pyftpdlib` instance for both plain and TLS.

**UI** — `app.run_test()` pilot: `f9` opens the dialog, a bad password shows the specific error in the
pane subtitle, a good one lists the remote directory, `tab` still switches panes while a remote
listing is in flight (proving the worker did not block the loop), `ctrl+d` restores the local pane.

---

## 10. What this does not protect against

Write this in `store/vault.py`'s module docstring, not only here. The vault protects secrets **at
rest** against someone reading the DuckDB file. It does not protect against malware running as the
user while the vault is unlocked, a keylogger capturing the master password, or memory inspection of
the live process. Claiming more than that would be dishonest.
