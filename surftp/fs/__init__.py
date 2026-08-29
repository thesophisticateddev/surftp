"""Filesystem backends for the panes.

The ``FileSystem`` protocol here is the transport/UI seam: panes render
``FileEntry`` rows and never know which protocol produced them.
"""

from __future__ import annotations

from surftp.fs.local import LocalFileSystem
from surftp.fs.types import (
    AsyncFileReader,
    AsyncFileWriter,
    FileEntry,
    FileSystem,
    FileSystemError,
    format_modified,
    format_permissions,
    format_size,
    sort_entries,
)

__all__ = [
    "AsyncFileReader",
    "AsyncFileWriter",
    "FileEntry",
    "FileSystem",
    "FileSystemError",
    "LocalFileSystem",
    "format_modified",
    "format_permissions",
    "format_size",
    "sort_entries",
]
