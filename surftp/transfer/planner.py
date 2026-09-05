"""Walk a source tree and produce a flat list of ``TransferItem``.

The planner is separate from the engine for one reason worth stating: **the
total byte count must be known before the first byte moves**, or the progress
bar cannot show a percentage and the user cannot tell a stalled transfer from
a slow one. Walking is itself I/O and must be cancellable.

The planner produces directories first, in depth order, so the engine can
create the destination tree before any file in it starts. Files are then
listed in the order they were encountered (depth-first).
"""

from __future__ import annotations


from surftp.fs.types import FileEntry, FileSystem, FileSystemError
from surftp.transfer.types import TransferItem


async def plan_transfer(
    source: FileSystem,
    source_path: str,
    destination: FileSystem,
    destination_path: str,
    entries: list[FileEntry] | None = None,
) -> list[TransferItem]:
    """Walk ``source_path`` (or the given ``entries``) and produce a flat item list.

    If ``entries`` is provided, the planner uses those instead of calling
    ``source.list_directory``. This lets the UI pass the already-loaded listing
    from the pane, avoiding a redundant round trip.

    Directories are yielded before their contents, in depth order. The returned
    list is ready to hand to the engine: directories first, then files.

    ``destination`` is taken purely to build destination paths with
    ``destination.join_path``. The planner used to guess the destination's path
    syntax from the string it was handed; the backend already knows it, so it is
    asked instead. Nothing here reads or writes through ``destination``.
    """
    items: list[TransferItem] = []
    if entries is None:
        try:
            entries = await source.list_directory(source_path)
        except FileSystemError:
            return []

    for entry in entries:
        if entry.name == "..":
            continue
        src = entry.path
        dst = destination.join_path(destination_path, entry.name)
        if entry.is_dir:
            items.append(TransferItem(src, dst, size=0, is_directory=True))
            sub_items = await plan_transfer(source, src, destination, dst)
            items.extend(sub_items)
        else:
            items.append(TransferItem(src, dst, size=entry.size, is_directory=False))

    return items
