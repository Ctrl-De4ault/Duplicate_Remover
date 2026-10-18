"""Phase 2: find duplicate files by content.

Pipeline: collapse hardlinks -> group by size -> partial hash (head + tail)
-> full hash. Each stage only processes files that survived the previous one.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from pathlib import Path

from .models import DuplicateGroup, FileRecord

try:
    from blake3 import blake3 as _blake3
except ImportError:  # keeps the engine usable if blake3 is missing
    _blake3 = None

PARTIAL = 4096  # bytes hashed from the head and from the tail
CHUNK = 1024 * 1024
ProgressFn = Callable[[str, int, int], None]  # (stage, done, total)


def _new_hasher():
    return _blake3() if _blake3 else hashlib.blake2b(digest_size=32)


def _partial_hash(path: Path, size: int) -> str:
    h = _new_hasher()
    with open(path, "rb") as f:
        if size <= 2 * PARTIAL:  # tiny file: this IS the full content
            h.update(f.read())
        else:
            h.update(f.read(PARTIAL))
            f.seek(-PARTIAL, os.SEEK_END)
            h.update(f.read(PARTIAL))
    return h.hexdigest()


def _full_hash(path: Path, size: int) -> str:
    h = _new_hasher()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


class HashCache:
    """SQLite cache keyed by path; an entry is valid only while size and mtime match."""

    def __init__(self, db_path: str | Path):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS hashes ("
            "path TEXT, kind TEXT, size INTEGER, mtime REAL, hash TEXT, "
            "PRIMARY KEY (path, kind))"
        )

    @staticmethod
    def _key(rec: FileRecord) -> str:
        return os.path.normcase(str(rec.path))

    def get(self, rec: FileRecord, kind: str) -> str | None:
        row = self.conn.execute(
            "SELECT size, mtime, hash FROM hashes WHERE path=? AND kind=?",
            (self._key(rec), kind),
        ).fetchone()
        if row and row[0] == rec.size and row[1] == rec.mtime:
            return row[2]
        return None

    def set(self, rec: FileRecord, kind: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO hashes VALUES (?,?,?,?,?)",
            (self._key(rec), kind, rec.size, rec.mtime, value),
        )

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()


@dataclass
class DetectionResult:
    groups: list[DuplicateGroup] = field(default_factory=list)  # biggest savings first
    errors: list[tuple[Path, str]] = field(default_factory=list)
    hardlinks_collapsed: int = 0  # extra paths pointing at an already-seen physical file
    files_considered: int = 0

    @property
    def total_wasted_bytes(self) -> int:
        return sum(g.wasted_bytes for g in self.groups)


def _collapse_hardlinks(files: list[FileRecord]) -> tuple[list[FileRecord], int]:
    """Paths sharing (device, inode) are one physical file: deleting one frees no space."""
    seen: set[tuple[int, int]] = set()
    unique: list[FileRecord] = []
    collapsed = 0
    for f in sorted(files, key=lambda r: str(r.path)):
        if f.inode != 0:
            ident = (f.dev, f.inode)
            if ident in seen:
                collapsed += 1
                continue
            seen.add(ident)
        unique.append(f)
    return unique, collapsed


def _compute(files, kind, fn, cache, workers, errors, progress, stage) -> dict[FileRecord, str]:
    out: dict[FileRecord, str] = {}
    todo = []
    for f in files:
        cached = cache.get(f, kind) if cache else None
        if cached is not None:
            out[f] = cached
        else:
            todo.append(f)
    total, done = len(files), len(out)
    if progress and done:
        progress(stage, done, total)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fn, f.path, f.size): f for f in todo}
        for fut in as_completed(futures):
            f = futures[fut]
            try:
                value = fut.result()
            except OSError as e:
                errors.append((f.path, str(e)))
            else:
                out[f] = value
                if cache:
                    cache.set(f, kind, value)
            done += 1
            if progress:
                progress(stage, done, total)
    if cache:
        cache.commit()
    return out


def find_duplicates(
    files: list[FileRecord],
    cache_path: str | Path | None = None,
    workers: int | None = None,
    on_progress: ProgressFn | None = None,
) -> DetectionResult:
    """Return groups of files with identical content (different names/folders allowed).

    cache_path   SQLite file for the hash cache; rescans skip unchanged files
    workers      hashing threads (default: min(8, cpu count)); hashing is I/O-bound
    on_progress  called as (stage, done, total) with stage "partial" or "full"
    """
    workers = workers or min(8, os.cpu_count() or 4)
    result = DetectionResult(files_considered=len(files))
    cache = HashCache(cache_path) if cache_path else None
    try:
        unique, result.hardlinks_collapsed = _collapse_hardlinks(files)

        by_size: dict[int, list[FileRecord]] = defaultdict(list)
        for f in unique:
            by_size[f.size].append(f)
        candidates = [f for g in by_size.values() if len(g) > 1 for f in g]

        partial = _compute(candidates, "partial", _partial_hash, cache, workers,
                           result.errors, on_progress, "partial")
        by_partial: dict[tuple[int, str], list[FileRecord]] = defaultdict(list)
        for f in candidates:
            if f in partial:
                by_partial[(f.size, partial[f])].append(replace(f, partial_hash=partial[f]))

        final: list[tuple[str, list[FileRecord]]] = []
        need_full: list[FileRecord] = []
        for (size, phash), grp in by_partial.items():
            if len(grp) < 2:
                continue
            if size <= 2 * PARTIAL:  # partial hash already covered the whole file
                final.append((phash, [replace(f, full_hash=phash) for f in grp]))
            else:
                need_full.extend(grp)

        full = _compute(need_full, "full", _full_hash, cache, workers,
                        result.errors, on_progress, "full")
        by_full: dict[tuple[int, str], list[FileRecord]] = defaultdict(list)
        for f in need_full:
            if f in full:
                by_full[(f.size, full[f])].append(replace(f, full_hash=full[f]))
        for (_, fhash), grp in by_full.items():
            if len(grp) >= 2:
                final.append((fhash, grp))

        groups = [DuplicateGroup(hash=h, files=sorted(g, key=lambda r: str(r.path))) for h, g in final]
        groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
        result.groups = groups
    finally:
        if cache:
            cache.close()
    return result
