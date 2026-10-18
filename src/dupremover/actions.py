"""Phase 4: carry out the plan produced by the rule engine, safely.

Safety layers
-------------
1. Dry run is the default: nothing on disk changes unless dry_run=False.
2. Just before touching each file it is re-checked: still a regular file,
   same size and modified time as at scan time, not the same physical file
   as the kept copy, and (by default) byte-for-byte identical to the kept copy.
   Any failed check skips that file.
3. If the kept copy is missing or changed, the whole group is skipped.
4. Hardlink/symlink replacement is atomic: the link is built under a temporary
   name and swapped in with os.replace, so the path never disappears.
5. Permanent delete needs confirm_permanent=True.
6. Every real action is appended to a JSONL log as it happens, so even a
   crash leaves an accurate record. undo() reads that log.

Undo support: QUARANTINE (moved back), HARDLINK and SYMLINK (independent copy
recreated from the kept file, original modified time restored).
RECYCLE and DELETE cannot be undone by DupRemover (use the Recycle Bin).
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .models import ActionKind, DuplicateGroup, FileRecord

try:
    from send2trash import send2trash as _send2trash
except ImportError:
    _send2trash = None

CHUNK = 1024 * 1024
ProgressFn = Callable[[int, int], None]  # (done, total)


def default_data_dir() -> Path:
    return Path.home() / ".dupremover"


@dataclass
class ActionOutcome:
    path: Path
    action: ActionKind
    status: str  # "done" | "dry_run" | "skipped" | "failed"
    detail: str = ""
    dest: Path | None = None  # where a quarantined file went
    size: int = 0


@dataclass
class ExecutionReport:
    session_id: str
    action: ActionKind
    dry_run: bool
    outcomes: list[ActionOutcome] = field(default_factory=list)
    log_path: Path | None = None
    cancelled: bool = False

    def _by(self, status: str) -> list[ActionOutcome]:
        return [o for o in self.outcomes if o.status == status]

    @property
    def done(self) -> list[ActionOutcome]:
        return self._by("done")

    @property
    def planned(self) -> list[ActionOutcome]:
        return self._by("dry_run")

    @property
    def skipped(self) -> list[ActionOutcome]:
        return self._by("skipped")

    @property
    def failed(self) -> list[ActionOutcome]:
        return self._by("failed")

    @property
    def bytes_recovered(self) -> int:
        return sum(o.size for o in self.done)

    @property
    def bytes_recoverable(self) -> int:
        return sum(o.size for o in self.planned)


@dataclass
class UndoReport:
    restored: list[Path] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)


# ----------------------------------------------------------------- checks

def _same_content(a: Path, b: Path) -> bool:
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            ca, cb = fa.read(CHUNK), fb.read(CHUNK)
            if ca != cb:
                return False
            if not ca:
                return True


def _keep_problem(keep: FileRecord) -> str | None:
    p = keep.path
    try:
        if os.path.islink(p) or not os.path.isfile(p):
            return "kept copy is missing or not a regular file"
        st = os.stat(p)
    except OSError as e:
        return f"kept copy cannot be read ({e})"
    if st.st_size != keep.size or st.st_mtime != keep.mtime:
        return "kept copy changed since the scan"
    return None


def _preflight(rec: FileRecord, keep: FileRecord, verify: bool) -> str | None:
    """Return a reason to skip this file, or None if it is safe to act on."""
    p = rec.path
    if os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(keep.path)):
        return "this is the kept copy"
    try:
        if os.path.islink(p) or not os.path.isfile(p):
            return "no longer a regular file"
        st, ks = os.stat(p), os.stat(keep.path)
    except OSError as e:
        return f"cannot read ({e})"
    if st.st_size != rec.size or st.st_mtime != rec.mtime:
        return "changed since the scan"
    if st.st_ino and (st.st_dev, st.st_ino) == (ks.st_dev, ks.st_ino):
        return "already the same physical file as the kept copy"
    if verify:
        try:
            if not _same_content(p, keep.path):
                return "content differs from the kept copy"
        except OSError as e:
            return f"cannot compare ({e})"
    return None


# ----------------------------------------------------------------- actions

def _tmp_name(p: Path, session: str) -> Path:
    return p.with_name(f".{p.name}.dupremover-{session[-6:]}.tmp")


def _swap_in_link(p: Path, keep: Path, session: str, symlink: bool) -> None:
    tmp = _tmp_name(p, session)
    try:
        if symlink:
            os.symlink(os.path.abspath(keep), tmp)
        else:
            os.link(keep, tmp)
        os.replace(tmp, p)  # atomic: p is never missing
    finally:
        if os.path.lexists(tmp):
            os.remove(tmp)


def _apply(action: ActionKind, rec: FileRecord, keep: FileRecord, session: str,
           qroot: Path, index: int) -> Path | None:
    p = rec.path
    if action is ActionKind.RECYCLE:
        _send2trash(str(p))
    elif action is ActionKind.DELETE:
        os.remove(p)
    elif action is ActionKind.QUARANTINE:
        dest = qroot / session / f"{index:06d}_{p.name}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dest))
        return dest
    elif action is ActionKind.HARDLINK:
        _swap_in_link(p, keep.path, session, symlink=False)
    elif action is ActionKind.SYMLINK:
        _swap_in_link(p, keep.path, session, symlink=True)
    return None


class _Log:
    def __init__(self, path: Path, header: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._f = open(path, "a", encoding="utf-8")
        self.write(header)

    def write(self, obj: dict) -> None:
        self._f.write(json.dumps(obj) + "\n")
        self._f.flush()  # every entry hits the file immediately

    def close(self) -> None:
        self._f.close()


def execute(
    groups: Iterable[DuplicateGroup],
    action: ActionKind,
    *,
    dry_run: bool = True,
    quarantine_dir: str | Path | None = None,
    log_dir: str | Path | None = None,
    verify_content: bool = True,
    confirm_permanent: bool = False,
    on_progress: ProgressFn | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ExecutionReport:
    """Apply `action` to every file in each group's `to_remove`.

    dry_run            True (default) only reports what would happen
    quarantine_dir     root for QUARANTINE (default ~/.dupremover/quarantine)
    log_dir            where the undo log goes (default ~/.dupremover/logs)
    verify_content     byte-compare each file to the kept copy first (default True)
    confirm_permanent  required for DELETE when dry_run is False
    on_progress        called as (done, total)
    should_cancel      polled between files; returning True stops cleanly
    """
    groups = list(groups)
    if action is ActionKind.DELETE and not dry_run and not confirm_permanent:
        raise ValueError("permanent delete needs confirm_permanent=True")
    if action is ActionKind.RECYCLE and not dry_run and _send2trash is None:
        raise RuntimeError("send2trash is not installed (pip install send2trash)")

    session = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    data_dir = default_data_dir()
    qroot = Path(quarantine_dir) if quarantine_dir else data_dir / "quarantine"
    report = ExecutionReport(session_id=session, action=action, dry_run=dry_run)
    log = None
    if not dry_run:
        report.log_path = Path(log_dir or data_dir / "logs") / f"{session}.jsonl"
        log = _Log(report.log_path, {
            "type": "session", "id": session, "action": action.value,
            "started": datetime.now().isoformat(timespec="seconds"),
            "quarantine_dir": str(qroot),
        })

    total = sum(len(g.to_remove) for g in groups)
    done = index = 0

    def record(rec: FileRecord, keep: FileRecord | None, status: str, detail: str = "",
               dest: Path | None = None) -> None:
        nonlocal done
        report.outcomes.append(ActionOutcome(rec.path, action, status, detail, dest, rec.size))
        if log and status != "dry_run":
            log.write({"type": "entry", "path": str(rec.path),
                       "keep": str(keep.path) if keep else None,
                       "action": action.value, "status": status, "detail": detail,
                       "dest": str(dest) if dest else None, "size": rec.size,
                       "mtime": rec.mtime,
                       "time": datetime.now().isoformat(timespec="seconds")})
        done += 1
        if on_progress:
            on_progress(done, total)

    try:
        for g in groups:
            keep = g.keep
            problem = "no kept copy was chosen for this group" if keep is None else _keep_problem(keep)
            for rec in g.to_remove:
                if should_cancel and should_cancel():
                    report.cancelled = True
                    return report
                reason = problem or _preflight(rec, keep, verify_content)
                if reason:
                    record(rec, keep, "skipped", reason)
                    continue
                if dry_run:
                    record(rec, keep, "dry_run", f"would {action.value}")
                    continue
                index += 1
                try:
                    dest = _apply(action, rec, keep, session, qroot, index)
                except Exception as e:  # locked file, permissions, cross-drive link, ...
                    record(rec, keep, "failed", f"{type(e).__name__}: {e}")
                else:
                    record(rec, keep, "done", dest=dest)
    finally:
        if log:
            log.close()
    return report


# -------------------------------------------------------------------- undo

def _restore_copy(path: Path, keep: Path, mtime: float, session: str) -> None:
    tmp = _tmp_name(path, session)
    try:
        shutil.copyfile(keep, tmp)
        st = os.stat(tmp)
        os.utime(tmp, (st.st_atime, mtime))
        os.replace(tmp, path)
    finally:
        if os.path.lexists(tmp):
            os.remove(tmp)


def undo(log_path: str | Path) -> UndoReport:
    """Reverse a session from its log, newest action first. Safe to run twice."""
    report = UndoReport()
    entries, session = [], "undo"
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("type") == "session":
                session = obj["id"]
            elif obj.get("type") == "entry" and obj.get("status") == "done":
                entries.append(obj)

    for e in reversed(entries):
        path, action = Path(e["path"]), e["action"]
        try:
            if action == ActionKind.QUARANTINE.value:
                dest = Path(e["dest"])
                if not dest.exists():
                    report.skipped.append((path, "quarantined file is no longer there"))
                elif os.path.lexists(path):
                    report.skipped.append((path, "original location is occupied"))
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dest), str(path))
                    report.restored.append(path)
            elif action in (ActionKind.HARDLINK.value, ActionKind.SYMLINK.value):
                keep = Path(e["keep"])
                if not os.path.isfile(keep):
                    report.skipped.append((path, "kept file is missing, cannot recreate the copy"))
                    continue
                if action == ActionKind.HARDLINK.value:
                    still_link = os.path.isfile(path) and not os.path.islink(path) and os.path.samefile(path, keep)
                else:
                    still_link = os.path.islink(path) and os.path.realpath(path) == os.path.realpath(keep)
                if not still_link:
                    report.skipped.append((path, "path was changed after the action"))
                    continue
                _restore_copy(path, keep, e["mtime"], session)
                report.restored.append(path)
            elif action == ActionKind.RECYCLE.value:
                report.skipped.append((path, "restore it from the Recycle Bin"))
            else:
                report.skipped.append((path, "permanent deletes cannot be undone"))
        except Exception as ex:
            report.failed.append((path, f"{type(ex).__name__}: {ex}"))
    return report
