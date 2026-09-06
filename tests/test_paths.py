"""Cross-OS path handling.

These exist because the transfer planner used to *guess* the destination's path
syntax from the string it was handed, and the Windows branch delegated to
``os.path`` — the **client's** module, which is ``posixpath`` on Linux and
macOS. The branch was therefore a no-op on every non-Windows client, producing
mixed separators like ``C:\\Users\\me/file.txt``. Path syntax now belongs to the
backend, which is the only component that knows it for certain.
"""

from __future__ import annotations

import asyncio
import ntpath
import os
import posixpath
import sys

sys.path.insert(0, "/home/salman/Documents/surftp")

from surftp.fs import LocalFileSystem  # noqa: E402
from surftp.fs.types import FileEntry, display_name, remote_join  # noqa: E402
from surftp.transfer.planner import plan_transfer  # noqa: E402

failures = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record one assertion without aborting the run."""
    global failures
    if condition:
        print(f"  PASS  {label}")
    else:
        failures += 1
        print(f"  FAIL  {label}  {detail}")


class FakeRemote:
    """A stand-in remote backend: POSIX wire rules, no network."""

    label = "fake@remote"

    def join_path(self, base: str, name: str) -> str:
        return remote_join(base, name)

    async def list_directory(self, path: str) -> list[FileEntry]:
        return []

    async def parent_of(self, path: str) -> str:
        return posixpath.dirname(path) or "/"

    async def is_directory(self, path: str) -> bool:
        return True


print("\n[1] remote_join uses POSIX rules for POSIX paths")
check("plain remote path", remote_join("/var/www", "index.html") == "/var/www/index.html")
check("root", remote_join("/", "f.txt") == "/f.txt")
check("empty base", remote_join("", "f.txt") == "f.txt")
check("OpenSSH-for-Windows style stays POSIX",
      remote_join("/C:/Users/me", "f.txt") == "/C:/Users/me/f.txt",
      remote_join("/C:/Users/me", "f.txt"))

print("\n[2] a Windows-reporting server gets ntpath, on ANY client OS")
# The old code called os.path here, which IS posixpath on Linux/macOS, so the
# whole branch did nothing. ntpath is explicit and platform-independent.
check("drive-letter base", remote_join("C:\\Users\\me", "f.txt") == "C:\\Users\\me\\f.txt",
      remote_join("C:\\Users\\me", "f.txt"))
check("UNC base", remote_join("\\\\srv\\share", "f.txt") == "\\\\srv\\share\\f.txt",
      remote_join("\\\\srv\\share", "f.txt"))
check("no mixed separators", "/" not in remote_join("C:\\Users\\me", "f.txt"),
      remote_join("C:\\Users\\me", "f.txt"))
check("matches ntpath exactly regardless of client",
      remote_join("C:\\Users\\me", "f.txt") == ntpath.join("C:\\Users\\me", "f.txt"))

print("\n[3] a backslash in a POSIX filename is NOT a platform signal")
check("backslash mid-path stays POSIX",
      remote_join("/srv/odd\\name", "f.txt") == "/srv/odd\\name/f.txt",
      remote_join("/srv/odd\\name", "f.txt"))
check("relative path with backslash stays POSIX",
      remote_join("docs\\notes", "f.txt") == "docs\\notes/f.txt",
      remote_join("docs\\notes", "f.txt"))

print("\n[4] LocalFileSystem uses the client's own rules")
local = LocalFileSystem()
check("local join matches os.path", local.join_path("/tmp/a", "b.txt") == os.path.join("/tmp/a", "b.txt"))

print("\n[5] the planner asks the destination, it does not guess")
entries = [
    FileEntry(name="a.txt", path="/src/a.txt", is_dir=False, size=10, modified=0.0),
    FileEntry(name="..", path="/", is_dir=True, size=0, modified=0.0),
]
items = asyncio.run(plan_transfer(local, "/src", FakeRemote(), "/var/www", entries=entries))
check("parent link skipped", len(items) == 1, str(items))
check("destination built by the remote backend",
      items[0].destination_path == "/var/www/a.txt", items[0].destination_path)

win_items = asyncio.run(plan_transfer(local, "/src", FakeRemote(), "C:\\inetpub", entries=entries))
check("windows destination uses backslash even on this client",
      win_items[0].destination_path == "C:\\inetpub\\a.txt", win_items[0].destination_path)

local_items = asyncio.run(plan_transfer(FakeRemote(), "/src", local, "/tmp/dest", entries=entries))
check("local destination uses local rules",
      local_items[0].destination_path == os.path.join("/tmp/dest", "a.txt"),
      local_items[0].destination_path)

print("\n[6] display_name handles either separator (labels only)")
check("posix", display_name("/var/www/index.html") == "index.html")
check("windows", display_name("C:\\Users\\me\\file.txt") == "file.txt",
      display_name("C:\\Users\\me\\file.txt"))
check("unc", display_name("\\\\srv\\share\\f.txt") == "f.txt")
check("trailing slash", display_name("/a/dir/") == "dir")
check("bare name", display_name("plain.txt") == "plain.txt")

print("\n[7] the old guessing helper is gone")
import surftp.transfer.planner as planner_mod  # noqa: E402
import surftp.app as app_mod  # noqa: E402
check("planner._join_path removed", not hasattr(planner_mod, "_join_path"))
check("app._join_dest_path removed", not hasattr(app_mod, "_join_dest_path"))

print(f"\n{'ALL PATH CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
