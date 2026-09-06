---
type: "query"
date: "2026-09-02T20:01:15.432819+00:00"
question: "What is the exact relationship between SFTPFileSystem and transfer/planner._join_path?"
contributor: "graphify"
source_nodes: ["_join_path", "SFTPFileSystem", "plan_transfer", "FileSystem", "LocalFileSystem"]
---

# Q: What is the exact relationship between SFTPFileSystem and transfer/planner._join_path?

## Answer

Expanded from original query via vocab: [join, path, posix, separator, destination, planner, sftp, remote, drive, local]. Then traversed: graphify path found NO direct edge — 4 hops, meeting only via a shared FileSystemError import, which is why the extractor marked the relation AMBIGUOUS. _join_path (planner.py:58, degree 4, called only by plan_transfer) guesses the destination OS from path syntax; SFTPFileSystem and FTPFileSystem assert posixpath unconditionally while LocalFileSystem uses os.path. Verified bug: the Windows branch delegates to os.path, which is the LOCAL platform's module — on Linux os.path IS posixpath, so the branch is a no-op and yields mixed separators (C:\\Users\\me/file.txt). It only works by accident on a Windows client. Root cause is architectural: the FileSystem protocol owns parent_of but not join, so the planner has to guess what the backend already knows. Fix: add join_path to the FileSystem protocol (posixpath for SFTP/FTP, os.path for Local) and have the planner call destination.join_path().

## Source Nodes

- _join_path
- SFTPFileSystem
- plan_transfer
- FileSystem
- LocalFileSystem