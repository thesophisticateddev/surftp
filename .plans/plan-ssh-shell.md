# Plan: Interactive SSH Shell Alongside SFTP

**Goal:** run a real interactive shell on the remote host, in the TUI, without disturbing the SFTP
session it shares a host with. The shell lives in the bottom panel beside the transfer monitor, as a
tab per session.

**In scope:** opening a shell for a connected SSH session tab, a terminal emulator with colour and
full-screen application support (`vim`, `htop`, `less`), keyboard input, resize, scrollback, and the
process isolation that keeps a runaway command from freezing the app.

**Out of scope:** mouse reporting inside the shell, `sudo` password prompt detection, running a shell
against an FTP session (there is no shell to run), splitting one shell across both sides, and
persisting shell history across restarts.

---

## 0. Measurements first, because they overturn the obvious design

Measured on this machine (12 cores, loopback, asyncssh 2.24, `aes256-gcm`, 256 MB SFTP transfer):

| Scenario | SFTP throughput |
| --- | --- |
| SFTP alone | **371–398 MB/s** |
| SFTP while a shell channel floods, **same connection** | 159 MB/s (**−57%**) |
| SFTP while a shell channel floods, **separate connection, same process** | 172 MB/s (**−55%**) |
| SFTP while a shell channel floods but the client **stops reading** | **375 MB/s (−6%)** |

| Terminal emulation (`pyte`, 120×40) | Throughput |
| --- | --- |
| Plain text | 1.2 MB/s |
| ANSI-coloured output | 1.3 MB/s |
| Full-screen redraws (vim/htop-like) | 1.2 MB/s |

Three conclusions, and they decide the whole design:

1. **A second SSH connection buys nothing.** Flooding on its own connection cost 55%, versus 57% on
   the shared one. The contention is not the SSH connection — it is the single Python process doing
   decryption and event-loop work. So "put the SSH session in another process for isolation" would
   pay for re-authentication and get ~2% back.
2. **Terminal emulation is the real bottleneck — by 300×.** `pyte` manages ~1.2 MB/s while SFTP moves
   ~380 MB/s. Emulation is what will freeze the UI, not SSH. **This is where isolation belongs.**
3. **Back-pressure is what makes a runaway command harmless.** With the client simply not reading,
   SSH flow control stopped the server after ~6.4 MB and SFTP ran at 375 MB/s — a 6% cost instead of
   57%. A terminal that refuses to read faster than it can render is self-defending, which is exactly
   how a real terminal behaves.

### The recommendation, and where it differs from the request

You asked for the SSH session to run in a **separate process** so it cannot disrupt SFTP. The instinct
is right and the measurements support it — but the thing that needs isolating is **the terminal
emulator, not the SSH connection**. So:

* **The shell channel stays on the existing authenticated `SSHSession`,** as a second channel next to
  SFTP. This is the "one connection, many channels" rule in `CLAUDE.md`, and it is what avoids a
  second authentication. A child process holding its own connection would have to re-authenticate,
  which means either re-prompting the user or **passing a plaintext secret across a process
  boundary** — a real security regression, bought for a 2% throughput difference that the numbers say
  does not exist.
* **A child process runs `pyte` and returns rendered screen grids.** That is the 1.2 MB/s component,
  and it is pure Python, so a thread would not help — the GIL makes a process the only real
  isolation. This is the separation you asked for, applied where it pays.
* **The parent reads the shell channel only as fast as the child consumes it.** SSH flow control then
  throttles the server at source, so `cat /dev/urandom` costs 6%, not 57%.

Net effect: a shell cannot freeze the UI, cannot meaningfully slow a transfer, and cannot force a
second login.

---

## 1. Module layout

```
surftp/shell/
  __init__.py
  protocol.py     # the parent<->child message schema, one place, msgpack-encoded
  emulator.py     # CHILD PROCESS entry point: pyte + frame coalescing. No SSH, no Textual.
  session.py      # ShellSession: owns the SSH channel, the child, and the back-pressure window
surftp/widgets/
  terminal.py     # TerminalView: paints a Frame, translates key events into bytes
  bottom.py       # the tabbed bottom panel (Transfers | Shell …), replacing the bare TransferPanel
```

`emulator.py` must import neither `asyncssh` nor `textual`. It is spawned, so every import it names
is paid again per shell; keeping it to `pyte` + `msgpack` keeps start-up near 100 ms rather than near
a second.

**Dependency:** `pyte` (pure Python, no transitive dependencies). `msgpack` is already installed.
Writing a VT100 emulator by hand is not a lightweight alternative — it is a subsystem with decades of
edge cases, and getting it subtly wrong corrupts the user's screen rather than failing loudly.

---

## 2. The child process

Spawned with `multiprocessing.get_context("spawn")` — `fork` would clone the parent's asyncio loop,
its open SSH sockets and the unlocked vault's DEK into a second process, which is both a correctness
and a security problem.

Transport is a `Pipe`, framed with **msgpack, not pickle**. Pickle executes code on load; the child is
our own, but a fixed schema costs nothing and removes the question.

```python
# parent -> child
{"t": "feed",   "d": <bytes>}            # raw bytes off the SSH channel
{"t": "resize", "cols": int, "rows": int}
{"t": "quit"}

# child -> parent
{"t": "frame",  "seq": int, "cursor": [col, row], "rows": [[<run>, ...], ...]}
{"t": "ack",    "bytes": int}            # how much of `feed` has been consumed
```

A `<run>` is `[text, fg, bg, flags]` — a horizontal span sharing one style. Sending runs rather than
cells keeps a 120×40 frame in the low kilobytes, so a 30 fps cap is ~100 KB/s of IPC.

The child **coalesces**: it drains its whole inbox, feeds it all to `pyte`, and emits *one* frame at
most every 33 ms. Intermediate states of a scrolling build log are never rendered, because nobody can
read them — this is the single biggest reason the shell will feel fast.

---

## 3. `ShellSession` — the parent side

```python
class ShellSession:
    """One remote shell: an SSH channel, a child emulator, and the window between them."""

    @classmethod
    async def start(cls, session: SSHSession, cols: int, rows: int) -> ShellSession: ...
    async def send_input(self, data: bytes) -> None: ...
    async def resize(self, cols: int, rows: int) -> None: ...
    async def close(self) -> None: ...
    @property
    def exited(self) -> bool: ...
```

* Opens the channel with `session.connection.create_process(term_type="xterm-256color",
  term_size=(cols, rows))` — a channel on the **existing** connection, so no re-auth.
* **Back-pressure is the point.** Track bytes sent to the child minus bytes acked. Above
  `MAX_UNACKED = 256 KB`, stop reading the channel entirely. asyncssh then lets the window close and
  the server stops sending. Measured: the server queues ~6.4 MB and gives up, and SFTP keeps 94% of
  its throughput. **Never read the channel into an unbounded buffer** — that converts a remote `cat`
  into local memory exhaustion, and it is exactly what the −57% row above looks like.
* Frames arrive from the child on a `@work(thread=True)` reader that posts a Textual message per
  frame. A blocking pipe read on the event loop would defeat the whole design.

---

## 4. Widget and placement

The bottom panel becomes tabbed, as you asked: **Transfers | Shell: \<session\> | …**, one shell tab
per connected session tab. `TransferPanel` moves inside it unchanged.

`TerminalView` paints the last `Frame` — a `Strip` per row built from the style runs, with the cursor
as a reverse-video cell. It holds no emulator state of its own; it is a display for whatever the child
last sent, which is what keeps it cheap.

Only one shell per session tab. Opening a shell for an FTP tab is refused with a message that says
why, rather than a disabled control the user cannot explain.

---

## 5. Keyboard: the part most likely to go wrong

**The app's own bindings will shadow the shell.** `tab`, `ctrl+d`, `ctrl+w`, `ctrl+pageup/pagedown`
are declared `priority=True` (they had to be — Textual's focus handling was swallowing them), and
`escape` cancels a transfer. Inside a shell those keys mean tab-completion, EOF, delete-word, and
*the* key vim needs. If this is not handled, the shell is unusable in a way that will look like a
Textual bug.

The fix is Textual's own mechanism, not a workaround: implement `App.check_action()` to return
`False` for pane and transfer actions while `TerminalView` has focus. A disabled binding falls through
to the focused widget, so the shell receives the raw key.

Because that swallows the app's normal keys, there must be **one key that always escapes**, and it
must be one no shell wants: **`f10` returns focus to the file panes.** It is shown in the panel's
border so a user who has trapped themselves can find it without the manual.

Key translation lives in one table in `terminal.py`: printable characters as UTF-8; `enter` → `\r`;
`backspace` → `\x7f`; arrows/home/end/page keys → their xterm sequences; `ctrl+<letter>` → the control
byte. Textual reports these on `event.key` / `event.character`.

**Resize:** on widget resize, send `SSHWriter.change_terminal_size()` *and* the child a `resize`.
Both, or the remote's idea of the screen diverges from the emulator's and full-screen apps corrupt.

---

## 6. Lifecycle and failure

* The shell belongs to its session tab. Closing the tab closes the shell; closing the shell leaves
  SFTP untouched.
* **Kill the child, always.** `SessionTabs.on_unmount` already closes connections because an
  app-level hook runs after its children are gone — the child process needs the same treatment, in
  the same place. A leaked emulator process is worse than the leaked SSH connection that bug caused,
  because it survives the app.
* Child crash → the tab shows "the terminal emulator stopped" with a Restart action; the SSH session
  and any running transfer are unaffected. That independence is the whole point of the split.
* Remote shell exits (`exit`, `Ctrl-D`) → show the exit status and keep the tab until dismissed, so a
  failed command's output is still readable.
* Cap shells at one per session tab, and reuse the existing 8-session cap.

---

## 7. Security

* The shell stream carries whatever the user types, including passwords typed at a `sudo` prompt.
  **It is never logged, never written to disk, and never included in an error message.** The child
  writes nothing but frames back.
* `spawn`, not `fork`, so the vault's DEK and the SSH socket are not duplicated into the child.
* The child gets terminal bytes only — never a credential, a profile, or a vault handle. It cannot
  authenticate anything, which is a direct consequence of leaving the SSH channel in the parent.
* Host-key verification is unchanged: the shell rides a connection that was already verified.

---

## 8. Verification

**Unit (no network):** feed `emulator.py` recorded byte streams and assert the resulting grid —
plain text, colours, a full-screen redraw, a resize mid-stream. Round-trip every protocol message.
Assert the child emits at most one frame per 33 ms under a continuous feed.

**Integration (real SSH server, as the existing suites do):** asyncssh's server can host a real
process. Run `echo`, check the grid; run a command emitting ANSI colour, check the runs; send a
window-change and confirm the remote sees the new size.

**The isolation claim, measured — not asserted:** start a 256 MB SFTP transfer, flood the shell with
`yes` for its duration, and require the transfer to stay within ~10% of its solo throughput. This
number is the reason for the whole design, so it belongs in the suite where a regression trips it.
Also assert unacked bytes never exceed `MAX_UNACKED`.

**UI (pilot):** open a shell on a connected tab; type a command and see output; `f10` returns focus
and `tab` switches panes again; while the shell is focused, `escape` and `ctrl+d` reach the shell
rather than cancelling a transfer or closing the tab; closing the session tab terminates the child
(assert the pid is gone); quitting the app leaves no orphan process.

---

## 9. What this must not break

Same contract as the session-tabs work: **all six existing suites pass unmodified.** Specifically —
`active_pane` / `left_pane` / `right_pane` keep their meaning; `TransferPanel` keeps its API and its
`ctrl+t` binding, gaining a parent tab container only; no change to `fs/`, `net/connect.py`, the
vault, or the transfer engine. The only edit outside `surftp/shell/` and the two widgets is
`App.check_action()`, which changes nothing while the shell is unfocused.
