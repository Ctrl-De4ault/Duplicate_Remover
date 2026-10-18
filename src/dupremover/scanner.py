"""Phase 1: walk folders and collect FileRecords. Reads metadata only, never file contents."""
from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .models import FileRecord


@dataclass
class ScanResult:
    files: list[FileRecord] = field(default_factory=list)
    errors: list[tuple[Path, str]] = field(default_factory=list)  # (path, reason)
    skipped_symlinks: int = 0
    skipped_small: int = 0


def _is_excluded(path: Path, excludes: list[Path]) -> bool:
    return any(path == ex or ex in path.parents for ex in excludes)


def scan(
    folders: Iterable[Path | str],
    excludes: Iterable[Path | str] = (),
    exclude_names: Iterable[str] = (),
    min_size: int = 1,
    max_size: int | None = None,
    extensions: Iterable[str] | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> ScanResult:
    """Recursively scan folders.

    excludes       folders (full paths) that are skipped entirely
    exclude_names  folder names skipped wherever they appear (e.g. ".git")
    min_size       files smaller than this are ignored (default 1 skips empty files)
    max_size       files larger than this are ignored (None = no limit)
    extensions     only keep these extensions, e.g. {".jpg", ".png"} (None = all)
    on_progress    called with the running file count (for the UI progress display)

    Symlinks are never followed. Unreadable paths are recorded in result.errors
    and the scan continues. Overlapping input folders never produce the same
    file twice.
    """
    result = ScanResult()
    ex_paths = [Path(os.path.abspath(p)) for p in excludes]
    ex_names = {n.lower() for n in exclude_names}
    exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions} if extensions else None
    seen: set[str] = set()  # normalized paths already recorded

    for root in folders:
        root = Path(os.path.abspath(root))
        if not root.is_dir():
            result.errors.append((root, "not a folder or not accessible"))
            continue
        stack = [root]
        while stack:
            current = stack.pop()
            if _is_excluded(current, ex_paths):
                continue
            try:
                entries = list(os.scandir(current))
            except OSError as e:
                result.errors.append((current, str(e)))
                continue
            for entry in entries:
                path = Path(entry.path)
                try:
                    if entry.is_symlink():
                        result.skipped_symlinks += 1
                    elif entry.is_dir(follow_symlinks=False):
                        if entry.name.lower() not in ex_names:
                            stack.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        _handle_file(path, result, seen, min_size, max_size, exts, on_progress)
                except OSError as e:
                    result.errors.append((path, str(e)))
    return result


def _handle_file(path, result, seen, min_size, max_size, exts, on_progress) -> None:
    key = os.path.normcase(str(path))
    if key in seen:
        return
    if exts is not None and path.suffix.lower() not in exts:
        return
    st = os.stat(path)  # full stat: DirEntry.stat() lacks inode/device on Windows
    if st.st_size < min_size or (max_size is not None and st.st_size > max_size):
        result.skipped_small += 1
        return
    seen.add(key)
    result.files.append(
        FileRecord(
            path=path,
            size=st.st_size,
            mtime=st.st_mtime,
            ctime=getattr(st, "st_birthtime", st.st_ctime),  # creation time where available
            dev=st.st_dev,
            inode=st.st_ino,
        )
    )
    if on_progress:
        on_progress(len(result.files))
