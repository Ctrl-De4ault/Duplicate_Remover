"""Core data model for DupRemover. No I/O and no UI code lives here."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


@dataclass(frozen=True)
class FileRecord:
    path: Path
    size: int
    mtime: float
    ctime: float
    dev: int  # (dev, inode) identifies a physical file:
    inode: int  # two paths sharing both are hardlinks to ONE file
    partial_hash: str | None = None
    full_hash: str | None = None


@dataclass
class DuplicateGroup:
    hash: str
    files: list[FileRecord]
    keep: FileRecord | None = None  # set by the rule engine
    protected: list[FileRecord] = field(default_factory=list)  # never removed

    @property
    def to_remove(self) -> list[FileRecord]:
        safe = set(self.protected)
        return [f for f in self.files if f != self.keep and f not in safe]

    @property
    def size(self) -> int:
        return self.files[0].size if self.files else 0

    @property
    def wasted_bytes(self) -> int:
        """Space recoverable if only one copy is kept."""
        return self.size * (len(self.files) - 1)


class RuleKind(Enum):
    KEEP_NEWEST = "keep_newest"
    KEEP_OLDEST = "keep_oldest"
    KEEP_SHORTEST_PATH = "keep_shortest_path"
    PREFER_FOLDER = "prefer_folder"
    PROTECT_FOLDER = "protect_folder"
    FILTER = "filter"  # type / size / date filters


@dataclass
class Rule:
    kind: RuleKind
    params: dict = field(default_factory=dict)
    priority: int = 0  # lower runs first


class ActionKind(Enum):
    RECYCLE = "recycle"
    QUARANTINE = "quarantine"
    HARDLINK = "hardlink"
    SYMLINK = "symlink"
    DELETE = "delete"  # permanent


@dataclass
class Profile:
    name: str
    folders: list[Path]
    excludes: list[Path] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    action: ActionKind = ActionKind.RECYCLE  # safe default
    dry_run_confirmed: bool = False  # first run must be a preview
    exclude_names: list[str] = field(default_factory=list)  # folder names skipped anywhere
    min_size: int = 1
    max_size: int | None = None
    extensions: list[str] | None = None  # None = all file types
    quarantine_dir: Path | None = None
