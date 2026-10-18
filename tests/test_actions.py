import json
import os
import shutil

import pytest

from dupremover import actions
from dupremover.actions import execute, undo
from dupremover.detector import find_duplicates
from dupremover.models import ActionKind as A, Rule, RuleKind as K
from dupremover.rules import apply_rules
from dupremover.scanner import scan

CONTENT = b"duplicate payload " * 50


def make(tmp_path, spec):
    """spec: {relative path: (bytes, mtime)}; returns the created root."""
    root = tmp_path / "data"
    for rel, (data, mtime) in spec.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        os.utime(p, (mtime, mtime))
    return root


def plan(root, rules=None):
    files = scan([root]).files
    groups = find_duplicates(files).groups
    return apply_rules(groups, rules if rules is not None else [Rule(K.KEEP_OLDEST)]).actionable


def run(tmp_path, groups, action, **kw):
    kw.setdefault("dry_run", False)
    kw.setdefault("quarantine_dir", tmp_path / "q")
    kw.setdefault("log_dir", tmp_path / "logs")
    return execute(groups, action, **kw)


SPEC = {"a/keep.bin": (CONTENT, 100), "b/copy1.bin": (CONTENT, 200), "c/copy2.bin": (CONTENT, 300),
        "d/other.bin": (b"unique", 50)}


def test_dry_run_is_default_and_changes_nothing(tmp_path):
    root = make(tmp_path, SPEC)
    rep = execute(plan(root), A.QUARANTINE, quarantine_dir=tmp_path / "q", log_dir=tmp_path / "logs")
    assert rep.dry_run and len(rep.planned) == 2 and rep.done == []
    assert rep.bytes_recoverable == 2 * len(CONTENT)
    assert (root / "b/copy1.bin").exists() and (root / "c/copy2.bin").exists()
    assert rep.log_path is None and not (tmp_path / "logs").exists()


def test_quarantine_moves_files_keeps_one_and_writes_log(tmp_path):
    root = make(tmp_path, SPEC)
    rep = run(tmp_path, plan(root), A.QUARANTINE)
    assert (root / "a/keep.bin").exists()
    assert not (root / "b/copy1.bin").exists() and not (root / "c/copy2.bin").exists()
    assert len(rep.done) == 2 and rep.bytes_recovered == 2 * len(CONTENT)
    assert all(o.dest.read_bytes() == CONTENT for o in rep.done)
    lines = [json.loads(x) for x in rep.log_path.read_text().splitlines()]
    assert lines[0]["type"] == "session" and sum(x["type"] == "entry" for x in lines) == 2


def test_undo_quarantine_restores_content_and_mtime(tmp_path):
    root = make(tmp_path, SPEC)
    rep = run(tmp_path, plan(root), A.QUARANTINE)
    u = undo(rep.log_path)
    assert len(u.restored) == 2 and u.skipped == [] and u.failed == []
    assert (root / "b/copy1.bin").read_bytes() == CONTENT
    assert int((root / "b/copy1.bin").stat().st_mtime) == 200


def test_same_filename_in_different_folders_does_not_collide(tmp_path):
    root = make(tmp_path, {"a/x.bin": (CONTENT, 1), "b/x.bin": (CONTENT, 2), "c/x.bin": (CONTENT, 3)})
    rep = run(tmp_path, plan(root), A.QUARANTINE)
    assert len({o.dest for o in rep.done}) == 2
    undo(rep.log_path)
    assert all((root / f"{d}/x.bin").exists() for d in "abc")


def test_recycle_uses_send2trash(tmp_path, monkeypatch):
    root = make(tmp_path, SPEC)
    sent = []
    monkeypatch.setattr(actions, "_send2trash", lambda p: (sent.append(p), os.remove(p)))
    rep = run(tmp_path, plan(root), A.RECYCLE)
    assert len(sent) == 2 and len(rep.done) == 2 and not (root / "b/copy1.bin").exists()
    u = undo(rep.log_path)
    assert u.restored == [] and len(u.skipped) == 2  # cannot undo from here


def test_permanent_delete_requires_confirmation(tmp_path):
    root = make(tmp_path, SPEC)
    with pytest.raises(ValueError):
        run(tmp_path, plan(root), A.DELETE)
    assert (root / "b/copy1.bin").exists()
    rep = run(tmp_path, plan(root), A.DELETE, confirm_permanent=True)
    assert len(rep.done) == 2 and not (root / "b/copy1.bin").exists()
    assert (root / "a/keep.bin").exists()
    assert execute(plan(make(tmp_path / "x", SPEC)), A.DELETE).dry_run  # dry run needs no confirm


def test_hardlink_replacement_and_undo(tmp_path):
    root = make(tmp_path, SPEC)
    rep = run(tmp_path, plan(root), A.HARDLINK)
    keep = root / "a/keep.bin"
    assert len(rep.done) == 2 and os.path.samefile(keep, root / "b/copy1.bin")
    assert not list(root.rglob("*.tmp"))
    u = undo(rep.log_path)
    assert len(u.restored) == 2
    c = root / "b/copy1.bin"
    assert not os.path.samefile(keep, c) and c.read_bytes() == CONTENT and int(c.stat().st_mtime) == 200


def test_symlink_replacement_and_undo(tmp_path):
    root = make(tmp_path, SPEC)
    rep = run(tmp_path, plan(root), A.SYMLINK)
    if rep.failed:
        pytest.skip("symlinks not permitted here")
    c = root / "b/copy1.bin"
    assert os.path.islink(c) and c.read_bytes() == CONTENT
    undo(rep.log_path)
    assert not os.path.islink(c) and c.read_bytes() == CONTENT and int(c.stat().st_mtime) == 200


def test_file_changed_after_scan_is_skipped(tmp_path):
    root = make(tmp_path, SPEC)
    groups = plan(root)
    (root / "b/copy1.bin").write_bytes(CONTENT + b"edited")
    rep = run(tmp_path, groups, A.QUARANTINE)
    assert (root / "b/copy1.bin").exists()
    assert [o.detail for o in rep.skipped] == ["changed since the scan"]
    assert len(rep.done) == 1


def test_same_size_and_mtime_but_different_content_is_caught(tmp_path):
    root = make(tmp_path, SPEC)
    groups = plan(root)
    p = root / "b/copy1.bin"
    st = p.stat()
    p.write_bytes(b"Z" * len(CONTENT))  # same size ...
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))  # ... same mtime
    rep = run(tmp_path, groups, A.QUARANTINE)
    assert p.exists() and p.read_bytes() == b"Z" * len(CONTENT)
    assert [o.detail for o in rep.skipped] == ["content differs from the kept copy"]


def test_missing_keep_blocks_the_whole_group(tmp_path):
    root = make(tmp_path, SPEC)
    groups = plan(root)
    (root / "a/keep.bin").unlink()
    rep = run(tmp_path, groups, A.DELETE, confirm_permanent=True)
    assert rep.done == [] and len(rep.skipped) == 2
    assert (root / "b/copy1.bin").exists() and (root / "c/copy2.bin").exists()


def test_changed_keep_blocks_the_group(tmp_path):
    root = make(tmp_path, SPEC)
    groups = plan(root)
    (root / "a/keep.bin").write_bytes(b"different now")
    rep = run(tmp_path, groups, A.QUARANTINE)
    assert rep.done == [] and (root / "b/copy1.bin").exists()


def test_target_that_is_a_hardlink_of_keep_is_skipped(tmp_path):
    root = make(tmp_path, SPEC)
    groups = plan(root)
    (root / "b/copy1.bin").unlink()
    os.link(root / "a/keep.bin", root / "b/copy1.bin")  # now the same physical file
    rep = run(tmp_path, groups, A.DELETE, confirm_permanent=True)
    assert any("same physical file" in o.detail or "changed" in o.detail for o in rep.skipped)
    assert (root / "a/keep.bin").exists()


def test_vanished_target_does_not_stop_the_rest(tmp_path):
    root = make(tmp_path, SPEC)
    groups = plan(root)
    (root / "b/copy1.bin").unlink()
    rep = run(tmp_path, groups, A.QUARANTINE)
    assert len(rep.done) == 1 and len(rep.skipped) == 1


def test_failure_on_one_file_is_recorded_and_run_continues(tmp_path, monkeypatch):
    root = make(tmp_path, SPEC)
    real, calls = shutil.move, []

    def flaky(src, dst):
        calls.append(src)
        if len(calls) == 1:
            raise PermissionError("file is locked")
        return real(src, dst)

    monkeypatch.setattr(actions.shutil, "move", flaky)
    rep = run(tmp_path, plan(root), A.QUARANTINE)
    assert len(rep.failed) == 1 and "locked" in rep.failed[0].detail and len(rep.done) == 1


def test_protected_files_are_never_touched(tmp_path):
    root = make(tmp_path, SPEC)
    rules = [Rule(K.PROTECT_FOLDER, {"folder": root / "b"}), Rule(K.KEEP_OLDEST)]
    run(tmp_path, plan(root, rules), A.DELETE, confirm_permanent=True)
    assert (root / "b/copy1.bin").exists()  # protected, so it is the one kept
    assert not (root / "a/keep.bin").exists() and not (root / "c/copy2.bin").exists()


@pytest.mark.parametrize("action", [A.QUARANTINE, A.HARDLINK, A.DELETE])
def test_at_least_one_copy_always_survives(tmp_path, action):
    root = make(tmp_path, SPEC)
    groups = plan(root)
    run(tmp_path, groups, action, confirm_permanent=True)
    assert sum(1 for p in root.rglob("*.bin") if p.read_bytes() == CONTENT) >= 1
    assert all(os.path.exists(g.keep.path) for g in groups)


def test_undo_skips_when_original_location_is_occupied(tmp_path):
    root = make(tmp_path, SPEC)
    rep = run(tmp_path, plan(root), A.QUARANTINE)
    (root / "b/copy1.bin").write_bytes(b"someone made a new file here")
    u = undo(rep.log_path)
    assert len(u.restored) == 1 and u.skipped[0][1] == "original location is occupied"
    assert (root / "b/copy1.bin").read_bytes() == b"someone made a new file here"


def test_undo_twice_is_harmless(tmp_path):
    root = make(tmp_path, SPEC)
    rep = run(tmp_path, plan(root), A.QUARANTINE)
    undo(rep.log_path)
    second = undo(rep.log_path)
    assert second.restored == [] and second.failed == []


def test_progress_and_cancel(tmp_path):
    root = make(tmp_path, SPEC)
    seen = []
    rep = run(tmp_path, plan(root), A.QUARANTINE, on_progress=lambda d, t: seen.append((d, t)),
              should_cancel=lambda: len(seen) >= 1)
    assert rep.cancelled and seen == [(1, 2)] and len(rep.done) == 1
