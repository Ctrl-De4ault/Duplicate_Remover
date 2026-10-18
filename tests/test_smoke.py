from pathlib import Path

from dupremover.models import (
    ActionKind, DuplicateGroup, FileRecord, Profile, Rule, RuleKind,
)


def _rec(name: str) -> FileRecord:
    return FileRecord(Path(name), size=10, mtime=1.0, ctime=1.0, dev=1, inode=hash(name))


def test_profile_defaults_are_safe():
    p = Profile(name="t", folders=[Path(".")])
    assert p.action is ActionKind.RECYCLE
    assert p.dry_run_confirmed is False


def test_group_to_remove_excludes_keep():
    a, b, c = _rec("a"), _rec("b"), _rec("c")
    g = DuplicateGroup(hash="h", files=[a, b, c], keep=b)
    assert g.to_remove == [a, c]


def test_rule_defaults():
    r = Rule(kind=RuleKind.KEEP_NEWEST)
    assert r.params == {} and r.priority == 0
