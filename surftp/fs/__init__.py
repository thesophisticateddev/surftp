"""Filesystem backends for the panes.

The ``FileSystem`` protocol here is the transport/UI seam: panes render
``FileEntry`` rows and never know which protocol produced them.
"""

from __future__ import annotations

from surftp.fs.local import LocalFileSystem
from surftp.fs.types import (
    FileEntry,
    FileSystem,
    FileSystemError,
    format_modified,
    format_size,
    sort_entries,
)

__all__ = [
    "FileEntry",
    "FileSystem",
    "FileSystemError",
    "LocalFileSystem",
    "format_modified",
    "format_size",
    "sort_entries",
]
