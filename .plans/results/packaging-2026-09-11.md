# Result: Standalone Builds and Release Automation

Implements [`plan-packaging.md`](../plan-packaging.md) §5 and §6. §1–§4 (packaging metadata, the
runtime/dev dependency split, `freeze_support()` in the entry point, the checked-in spec) were already
in the tree; this work added the verification layer, the two workflows, the macOS signing path, and
the build documentation — and fixed a real defect in the spec found by actually running what it built.

## What was built

| File | Purpose |
| --- | --- |
| `surftp/selfcheck.py` | In-bundle verification, reached by the hidden `--self-check` flag |
| `tests/test_frozen.py` | Drives the built artifacts from outside; the packaging suite |
| `.github/workflows/test.yml` | Source CI: three OSes, every suite, plus the self-check |
| `.github/workflows/release.yml` | Four-target build matrix on `v*` tags → GitHub Release |
| `packaging/macos-sign.sh` | Codesign + notarise, skipping cleanly with no secrets |
| `packaging/entitlements.plist` | Hardened-runtime entitlements a frozen CPython needs |
| `docs/building.md` | How to build and verify your own binary |

Modified: `packaging/surftp.spec` (mode split, below), `surftp/__main__.py` (the hidden flag),
`tests/run_all.sh` (`PY` override for CI), `README.md`, `.gitignore`.

## Deviation from the plan: one artifact per invocation

**The plan's spec builds both artifacts in one pass. That does not work, and the failure is silent.**

The checked-in spec emitted a onefile `EXE` and a onedir `COLLECT` from a single `Analysis`. It built
with exit 0 and no warning. The onedir artifact was correct. The onefile artifact was not:

```
onedir  --version   surftp 0.1.0
onefile --version   [PYI-412426:ERROR] Failed to extract entry: libexpat.so.1.
onefile --self-check [PYI-412452:ERROR] Could not load PyInstaller's embedded PKG archive
onefile no strays   1 process(es) still running
```

Confirmed the cause by building onefile alone with the same inputs — it works. The two passes share
one `Analysis` and one workpath, and the onefile PKG comes out corrupt. The spec now takes
`SURFTP_BUILD_MODE=onedir|onefile`, rejects anything else, and CI invokes it twice with separate
`--workpath` values. Both artifacts now pass every check.

This is precisely the class of failure §5 of the plan predicted — a build reporting success while
shipping a binary that cannot start — and it was caught by the verification step, on its first run,
against code that had been checked in.

## Deviation: the self-check ships inside the application

`surftp/selfcheck.py` lives in the package, not in `tests/`. The checks §5 asks for — the CSS loaded,
both panes mounted, `spawn` producing exactly one child — can only be made from code executing inside
the bundle, with its own `sys._MEIPASS`, its own metadata and its own re-executing `spawn`. A test
file outside the bundle cannot reach there. The cost is a few kB and a hidden flag
(`help=argparse.SUPPRESS`); the benefit is that the release gate tests the artifact rather than a
proxy for it. `tests/test_frozen.py` invokes it and adds the two assertions only visible from outside:
that the process terminates rather than hanging, and that it leaves no strays.

The stray check matches the artifact's **absolute path**, not its name — this repository is itself a
directory called `surftp`, so a name match counted the test runner as a stray.

## Deviation: no stapling on macOS

The plan says "codesign with a Developer ID and notarise". The script signs and notarises but
deliberately does not staple: a notarisation ticket can only be attached to an `.app`, `.dmg` or
`.pkg`, and SURFTP ships a bare command-line binary — `xcrun stapler staple` fails with error 73.
Gatekeeper verifies notarisation online for these, so the submission is what matters.

The hardened runtime that notarisation requires also needs three entitlements
(`allow-jit`, `allow-unsigned-executable-memory`, `disable-library-validation`), or the signed,
notarised binary crashes on first launch: a frozen CPython allocates writable-executable memory for
cffi trampolines, and `argon2-cffi`, `bcrypt`, `cryptography` and `duckdb` all arrive through cffi or
a native extension.

## Addition: tag/version agreement

The release job fails if the tag does not match `surftp.__about__.__version__`. Without it the classic
mistake — tag `v0.2.0`, ship binaries that report `0.1.0` — produces a release nobody can trace back
to a commit. `__about__.py` stays the single source; the tag is checked against it, never copied into
the build config.

## Verification actually performed

On Linux (Ubuntu, glibc 2.39, Python 3.12.3), against a real build:

- `SURFTP_BUILD_MODE=onedir` and `=onefile` both build from the checked-in spec.
- `python tests/test_frozen.py` → **FROZEN SUITE PASSED**, 8 checks. For each artifact: `--version`
  returns `surftp 0.1.0`; the in-bundle self-check reports 9 modules imported, 171 CSS rules with both
  panes mounted, one emulator child with one frame and no strays, and the vault resolving to
  `~/.local/share/surftp/vault.duckdb`; zero stray processes after exit.
- `python -m surftp --self-check` passes on source too, so the two paths agree.
- Archive round trip rehearsed: `tar -czf` → unpack → the unpacked binary runs and the executable bit
  survives (which is why the Unix artifacts are `.tar.gz` and not `.zip`).
- `sha256sum` over the artifacts produces the per-target sums file the release job merges.
- Both workflow files parse as YAML and every step carries a `uses` or a `run`.
- `tests/run_all.sh` passes unchanged after the `__main__.py` and `PY` changes.

**Not verified by execution:** the Windows and macOS matrix legs, the signing/notarisation script, and
the GitHub Release publication — none can run on this machine. They are written against the documented
behaviour of the runners and `notarytool`, and the first tagged release is what exercises them; a
`workflow_dispatch` rehearsal builds and verifies artifacts on all four targets without publishing,
which is the cheap way to find out.

## Known risks

- **Runner availability.** `ubuntu-22.04` is chosen for glibc compatibility and `macos-13` for x86_64;
  both are older images and will eventually be retired. When that happens, move to the next-oldest
  available — not to `latest`, or Linux binaries silently stop starting on older distributions.
- `fail-fast: false` throughout, so one retired runner or one notary outage cannot hide the state of
  the other three targets.

## Follow-ups, not done here

- `actions/attest-build-provenance` on the release job, once the workflow has run green a few times.
  A few lines, and it makes "did this binary come from that commit?" answerable.
- Windows code signing, which needs a purchased certificate; until then SmartScreen will warn.
- The size question from §3 of the plan stands untouched: DuckDB is 57% of the bundle.
