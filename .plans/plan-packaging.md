# Plan: Standalone Builds and Release Automation

**Goal:** a downloadable SURFTP for Windows, Linux and macOS that runs with no Python installed, built
reproducibly by GitHub Actions and attached to every tagged release.

**In scope:** packaging metadata, a checked-in PyInstaller spec, the runtime/dev dependency split, the
per-OS build matrix, signing where it is required to run at all, and the release workflow.

**Out of scope:** OS package repositories (Homebrew, apt, winget, AUR) — worth doing later, but each
has its own submission process and all of them consume the artifacts this plan produces; and an
installer/MSI, since a terminal application is normally unpacked, not installed.

---

## 0. A trial build, run before writing this

Rather than plan against the documentation, I built the app with PyInstaller 6.22.2 on this machine.
Four things broke, and they shape everything below.

| Result | Measurement |
| --- | --- |
| `--onedir` bundle size | **105 MB** |
| `--onefile` bundle size | **43 MB** (compressed) |
| `--onedir` startup | **0.44–0.47 s** |
| `--onefile` startup | **1.40–1.52 s** (re-extracts every launch) |
| Largest single file | `_duckdb...so` at **60.4 MB — 57% of the bundle** |
| Next largest | `cryptography` `_rust.abi3.so` 14.4 MB, `libpython3.12.so` 9.1 MB |

### The four failures, in the order they appeared

1. **The frozen app fork-bombs.** `ShellSession` uses
   `multiprocessing.get_context("spawn")` (`surftp/shell/session.py:87`). Under PyInstaller, `spawn`
   re-executes *the bundle*, which runs the entry point again, which spawns again. A probe
   reproducing SURFTP's exact pattern produced a cascade and left **23 stray processes** behind after
   a single run. Adding `multiprocessing.freeze_support()` as the first statement of the entry point
   fixed it completely: one parent, one child, zero strays. **This is the single most important line
   in this plan** — without it the shipped app is a process bomb the moment a user opens a shell.

2. **Startup crash: `PackageNotFoundError: No package metadata was found for aioftp`.** aioftp reads
   its own distribution metadata at import time, and PyInstaller does not bundle `.dist-info` unless
   told. Fixed with `--copy-metadata aioftp`.

3. **Startup crash: `ModuleNotFoundError: No module named 'textual.widgets._tab_pane'`.** Textual
   lazy-loads widgets through `__getattr__` + `import_module`, which static analysis cannot see. Fixed
   with `--collect-submodules textual --collect-data textual`. This is why `SessionTabs` in particular
   would fail — the tab machinery is exactly the lazily-imported part.

4. **`--add-data` resolves relative to `--specpath`, not the working directory**, so the first build
   failed to find `app.tcss`. A checked-in `.spec` file with paths derived from `SPECPATH` removes the
   whole class of error, which is why §2 uses one instead of a long command line.

After all four fixes the frozen binary runs: `surftp 0.1.0`, and the TUI starts and stays up.

---

## 1. Prerequisites

### 1.1 Packaging metadata

There is no `pyproject.toml`. Add one: name, version read from `surftp/__about__.py` (single source —
do not duplicate the version string into the build config), description, license, `requires-python =
">=3.12"`, and a `surftp = "surftp.__main__:main"` console script. This also makes `pip install .`
work, which is the fallback for platforms the matrix does not cover.

### 1.2 Split the dependencies — currently the build would ship the dev toolchain

`requirements.txt` is a full `pip freeze`: **31 of its 51 lines are dev-only or transitive** —
`textual-dev`, `textual-serve`, the whole `aiohttp` stack, ten `tree-sitter-*` grammars, `pytest`.
None of it is imported by the app. Split into:

* `requirements.txt` — runtime only: `textual`, `asyncssh`, `aioftp`, `duckdb`, `cryptography`,
  `argon2-cffi`, `bcrypt`, `pyte`, `platformdirs`, `msgpack`.
* `requirements-dev.txt` — `-r requirements.txt` plus `textual-dev`, `textual-serve`, `pytest`,
  `pytest-asyncio`, `pyinstaller`.

The trial build still excluded them explicitly (`--exclude-module`) as a belt-and-braces measure;
keep those excludes in the spec, because a transitive pull-in should not silently add 20 MB.

### 1.3 `freeze_support()` in the entry point

```python
def main(argv: list[str] | None = None) -> int:
    # MUST be first: under PyInstaller `spawn` re-executes this binary, so
    # without this the shell's emulator child re-runs main() and forks again.
    multiprocessing.freeze_support()
    ...
```

Guard it with a test (§5) — this is invisible in a source checkout and catastrophic in a build.

---

## 2. The spec file

Check in `packaging/surftp.spec` rather than driving PyInstaller from a long command line, so the
build is identical locally and in CI, and so data paths are anchored to `SPECPATH`.

It must carry, at minimum:

```python
datas = [(str(ROOT / "surftp" / "app.tcss"), "surftp")]
datas += copy_metadata("aioftp")
hiddenimports = collect_submodules("textual")
datas += collect_data_files("textual")
excludes = ["textual_dev", "textual_serve", "aiohttp", "pytest", "tkinter", "test"]
```

Two artifacts per platform, because they serve different users:

| Artifact | Size | Startup | For |
| --- | --- | --- | --- |
| `onedir` (zipped) | 105 MB | 0.45 s | the default download; also what macOS signing and notarisation need |
| `onefile` | 43 MB | 1.5 s | convenience — one file to drop on a server |

A note on folklore: onefile plus `multiprocessing` is often said to be broken. I tested it — with
`freeze_support()` the child reuses the parent's extraction and starts promptly. Both formats work;
the trade is disk size against a 1-second startup penalty on every launch.

---

## 3. Size

105 MB is large for a terminal client whose stated constraint is staying lightweight, and **57% of it
is DuckDB** — a full analytical database engine used to store a handful of connection profiles.

Cheap reductions, in order of value:

1. `strip=True` in the spec (Linux/macOS): a few MB, free.
2. Excludes already listed: keeps the dev toolchain out.
3. **Do not use UPX.** It is the most common trigger for antivirus false positives on Windows, and a
   client that Defender quarantines is worse than a client that is 40 MB larger.

The honest observation, recorded for a future decision rather than acted on here: the vault needs
*key-value storage with transactions*. SQLite is in the standard library, would remove the single
largest dependency, and would cut the bundle by more than half. Migrating the store is out of scope
for a packaging plan — but if binary size matters to distribution, that is the lever, not compression
flags.

---

## 4. Per-platform specifics

### Linux
Build on the **oldest supported runner** (`ubuntu-22.04`), because PyInstaller bundles against the
build machine's glibc and a binary built on 24.04 will not start on 22.04. Ship `.tar.gz` (preserves
the executable bit; a `.zip` does not).

### macOS
* **Two separate builds, not universal2:** `macos-13` for x86_64 and `macos-14` for arm64. A
  universal binary requires universal wheels for every native dependency, and DuckDB and
  `cryptography` ship per-architecture wheels.
* **Signing is not optional for a downloaded artifact.** Unsigned, Gatekeeper refuses with "cannot be
  opened because the developer cannot be verified", and the fix (right-click → Open, or
  `xattr -d com.apple.quarantine`) is a support burden on every user. Codesign with a Developer ID and
  notarise. The workflow must skip signing gracefully when the secrets are absent (forks, PRs) rather
  than failing the build.

### Windows
* `windows-latest`, console subsystem (a TUI needs a console).
* **Expect SmartScreen and antivirus friction** on an unsigned PyInstaller binary — another reason to
  prefer the onedir zip over onefile, which is the shape heuristics flag hardest.
* Note in the README that Textual needs Windows Terminal; `conhost.exe` renders poorly.

---

## 5. Verification

The build is worthless if it produces a binary that fails on first contact, and every failure in §0
was a *runtime* failure that a successful build reported as success. So the workflow must **run the
built artifact**, not just produce it:

* `surftp --version` on every platform — catches missing metadata and hidden imports.
* **A frozen smoke test** that starts the app headlessly and asserts the CSS loaded and the two panes
  mounted — catches a missing `app.tcss` or a lazily-imported widget.
* **A frozen `spawn` test**: launch the child-process path and assert exactly one child appears and no
  strays remain. This is the `freeze_support()` regression guard, and it is the one that would have
  caught the process bomb.
* The existing suites run *before* packaging, on source, as they do today.

---

## 6. GitHub Actions

Two workflows:

**`.github/workflows/test.yml`** — on push and PR: matrix over the three OSes, Python 3.12, install
`requirements-dev.txt`, run `tests/run_all.sh`. Note the standalone suites bind loopback ports
(8021–8044) and start real SSH/FTP servers; that works on GitHub runners, but the ports must not
collide, so the matrix runs one job per OS rather than several in parallel on one runner.

**`.github/workflows/release.yml`** — on `v*` tags:

```yaml
strategy:
  fail-fast: false          # one platform failing must not hide the others
  matrix:
    include:
      - {os: ubuntu-22.04,  target: linux-x86_64}
      - {os: windows-latest, target: windows-x86_64}
      - {os: macos-13,      target: macos-x86_64}
      - {os: macos-14,      target: macos-arm64}
```

Steps: checkout → `setup-python@v5` (3.12, pip cache) → install `requirements-dev.txt` → **run the
tests** → `pyinstaller packaging/surftp.spec` → run the frozen smoke + spawn tests → sign/notarise on
macOS *if secrets exist* → archive (`.tar.gz` on Unix, `.zip` on Windows) → `sha256sum` → upload
artifact. A final job downloads all artifacts and creates the GitHub Release with the binaries and a
`SHA256SUMS` file.

Pin every action to a major version, and set `permissions: contents: write` only on the release job.

---

## 7. Security notes

* **Publishing checksums is the minimum.** Users download a 100 MB opaque binary that will hold their
  SSH credentials; `SHA256SUMS` in the release lets them verify what they ran.
* Nothing secret goes into the bundle. The vault lives in `platformdirs.user_data_dir` and
  `known_hosts` in `~/.ssh` — both resolved at runtime, both unaffected by freezing. Worth an explicit
  test that a frozen binary writes its vault to the user directory and *not* next to the executable.
* Signing secrets live in GitHub Secrets and must never be echoed; the macOS signing step needs
  `set +x` discipline around the keychain import.
* Consider build provenance (`actions/attest-build-provenance`) once the workflow is stable — it is a
  few lines and makes "did this binary come from that commit?" answerable.

---

## 8. What must not break

No change to application behaviour. `freeze_support()` is a no-op outside a frozen build; the
dependency split removes nothing the app imports; the spec is additive. The existing eight standalone
suites and four pytest suites must pass unchanged before any artifact is uploaded.
