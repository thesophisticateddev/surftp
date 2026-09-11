# SURFTP

A lightweight two-pane terminal client for **SFTP, FTP/FTPS, SSH and SCP**, in the style of Norton
Commander and Midnight Commander: local files on one side, the remote host on the other. Built with
[Textual](https://textual.textualize.io/).

Authenticate with a `.pem` private key or a username and password, and optionally save connection
profiles in a local DuckDB vault encrypted behind a master password.

```
┌─ /home/you/projects ─────────────┐┌─ you@server:22:/var/www ─────────┐
│ Name          Size    Modified   ││ Name          Size    Modified   │
│ ..            <DIR>              ││ ..            <DIR>              │
│ src           <DIR>   2026-08-28 ││ releases      <DIR>   2026-08-27 │
│ README.md     2.4 K   2026-08-28 ││ index.html    8.1 K   2026-08-26 │
└─ 12 items ───────────────────────┘└─ 34 items ───────────────────────┘
 tab Switch pane  enter Open dir  f9 Connect  ctrl+o Profiles  ? Help
```

## Install

### Download a binary (no Python needed)

Grab the archive for your platform from the [latest release](../../releases/latest) — Linux x86\_64,
Windows x86\_64, macOS x86\_64 and macOS arm64. Each is a self-contained build with CPython and every
dependency inside; there is nothing to install.

```bash
tar -xzf surftp-<version>-linux-x86_64.tar.gz
./surftp/surftp
```

Every release carries two builds per platform: the default directory build, and a `-onefile` build
that is one file to drop on a server at the cost of a second of startup while it unpacks itself.

**Verify what you downloaded before running it** — this program will hold your SSH credentials:

```bash
sha256sum -c SHA256SUMS.txt --ignore-missing
```

Windows binaries are unsigned, so SmartScreen warns on first run; use Windows Terminal rather than the
legacy console, which renders Textual poorly. macOS binaries are signed and notarised when the
project's signing secrets are configured, and will otherwise need a right-click → *Open*.

### From source

Requires **Python 3.12 or newer**, on Linux, macOS, or any platform with a terminal Textual supports.
Dependencies are deliberately few: `textual` for the UI, `asyncssh` (SSH/SFTP/SCP) and `aioftp`
(FTP/FTPS) for transport, and `duckdb` + `cryptography` + `argon2-cffi` + `bcrypt` for the credential
vault.

```bash
git clone <this repository>
cd surftp

python3 -m venv tenv
source tenv/bin/activate
pip install -r requirements.txt
```

### Build your own binary

```bash
pip install -r requirements-dev.txt
SURFTP_BUILD_MODE=onedir  pyinstaller --noconfirm --clean --workpath build/onedir packaging/surftp.spec
SURFTP_BUILD_MODE=onefile pyinstaller --noconfirm --workpath build/onefile packaging/surftp.spec
python tests/test_frozen.py     # run what you built — this is the part that matters
```

You can only build for the platform you are on. Full details, including macOS signing and the
per-platform pitfalls, are in **[docs/building.md](docs/building.md)**.

## Running

```bash
source tenv/bin/activate
python -m surftp
```

Both panes open on your current working directory. `python -m surftp --version` prints the version;
`--help` lists the options.

## Key bindings

| Key | Action |
| --- | --- |
| `tab` | Switch between the two panes |
| `enter` | Open the selected directory (or follow `..`) |
| `backspace` | Go to the parent directory |
| `ctrl+r` | Reload the current listing |
| `ctrl+h` | Show or hide dotfiles |
| `f9` | Connect the focused pane to a remote host |
| `ctrl+o` | Open the saved-profile picker |
| `ctrl+d` | Disconnect the focused pane, returning it to local disk |
| `ctrl+l` | Lock the credential vault |
| `?` | Full key-binding help panel |
| `ctrl+q` | Quit |

Every action applies to the **focused** pane — the one with the highlighted border.

## Connecting to a host

Press `f9` and fill in the dialog:

- **Protocol** — `SFTP`, `SCP` or `FTP`. The port follows your choice (22 / 22 / 21) until you type
  your own.
- **Host**, **Port**, **Username**
- **Remote path** *(optional)* — where the pane should open. Left blank, SURFTP asks the server where
  your login landed.
- **Authentication** — see below.
- **Save to vault** — unchecked by default, so a one-off connection never leaves a password on disk.

The focused pane switches to the remote host. Press `ctrl+d` to disconnect and return it to the local
directory you left.

### Authentication methods

**Private key file (`.pem`)** — the usual choice, and what cloud providers hand you. Give the path to
the key; SURFTP stores only that path, never the key itself. Supported formats are PKCS#1 and PKCS#8
PEM, OpenSSH, and PuTTY, so an AWS-issued `.pem` works as-is. Passphrase-protected keys are supported:
you are prompted only when the key actually needs one.

```
Authentication: Private key file (.pem)
Path:           /home/you/.ssh/prod-server.pem
Passphrase:     (leave blank if the key is unencrypted)
```

If the key file is readable by other users, SURFTP warns you (as OpenSSH does) but does not refuse.
Fix it with `chmod 600 your-key.pem`.

**Password** — a username and password. Available for every protocol.

**Private key stored in vault** — copies the key's contents into the encrypted vault, for a single
portable vault file rather than a key on disk. Opt-in; the dialog says so when you pick it.

**ssh-agent** — delegates to a running agent via `SSH_AUTH_SOCK`.

### A note on SCP

SCP transfers files but has no way to list a remote directory, so an SCP profile **browses over SFTP**
on the same connection and transfers with `scp`. It will not work against a server that has the SFTP
subsystem disabled — the pane tells you if that happens.

### A note on plain FTP

Plain FTP sends your username and password in clear text. Tick **Use TLS (FTPS)** unless you know the
server does not support it. SURFTP warns you in the dialog whenever TLS is off.

## Host key verification

The first time you connect to a host, SURFTP shows its SHA-256 fingerprint and asks whether to trust
it. Verify the fingerprint against one you got from the server's owner — not from the screen in front
of you — then choose *Trust*. Accepted keys are appended to your own `~/.ssh/known_hosts`, so trusting
a host here also trusts it for `ssh`.

If a host's key ever **changes**, the connection fails outright and there is no "accept anyway" button.
That is either a rebuilt server or someone intercepting your traffic. Confirm which, then remove the
stale line from `~/.ssh/known_hosts` yourself before reconnecting.

## The credential vault

Ticking **Save to vault** stores the connection profile and its secrets in a local DuckDB database:

```
~/.local/share/surftp/vault.duckdb      (Linux)
~/Library/Application Support/surftp/   (macOS)
```

The file is created readable only by you. Passwords, key passphrases and vault-stored keys are
encrypted with AES-256-GCM under a key derived from your master password with Argon2id; only the
encrypted form is ever written. Host names and usernames are stored in the clear so the profile picker
can list them before you unlock — **the vault protects your secrets, not the fact that you have an
account somewhere.**

You are asked for a master password the first time you save something, and once per session
afterwards. `ctrl+l` locks the vault again.

> **There is no recovery path.** Your master password is never stored anywhere, by design. If you lose
> it, every saved credential is unreadable and the vault has to be deleted and rebuilt. Put it in a
> password manager before you go any further.

What the vault does **not** protect against: anything running as you while the vault is unlocked —
malware, a keylogger capturing the master password, or inspection of the live process's memory. It
protects the file at rest.

## Development

```bash
textual run --dev surftp.app:SurfFTPApp   # hot CSS reload + devtools
textual console                           # in a second terminal: live logs
textual serve "python -m surftp"          # serve the TUI over HTTP
```

### Tests

```bash
./tests/run_all.sh                                       # every suite
PYTHONPATH=$PWD ./tenv/bin/python tests/test_vault.py    # one suite
./tenv/bin/python tests/test_frozen.py                   # the built binaries (see docs/building.md)
```

The suites are standalone scripts rather than pytest; each exits non-zero on failure and prints one
PASS/FAIL line per check. The integration suites start **real servers** on loopback — asyncssh's own
SSH/SFTP server and an aioftp server — because the bugs worth catching (host-key handling, key
passphrase classification, listing semantics) only show up against a real protocol exchange. They
redirect `HOME` to a temporary directory first, so they never touch your own `~/.ssh/known_hosts`.

## Project status

Working today: the two-pane browser, remote connections over SFTP/SCP/FTP/FTPS, `.pem` and password
authentication, host-key verification, and the encrypted credential vault.

Not built yet: **file transfers between the panes**, the SSH tunnel, and remote mutation (mkdir,
delete, rename). Design notes for the next steps live in `.plans/`.
