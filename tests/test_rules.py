import random
from datetime import datetime
from pathlib import Path

import pytest

from dupremover.models import DuplicateGroup, FileRecord, Rule, RuleKind as K
from dupremover.rules import apply_rules, validate_rules


def rec(path, mtime=100.0, ctime=100.0, size=100):
    return FileRecord(Path(path), size=size, mtime=mtime, ctime=ctime, dev=1, inode=abs(hash(path)) + 1)


def group(*files):
    return DuplicateGroup(hash="h", files=list(files))


def keep_of(files, rules):
    r = apply_rules([group(*files)], rules)
    g = (r.actionable + r.untouched)[0]
    return g.keep.path.as_posix()


def removed(files, rules):
    r = apply_rules([group(*files)], rules)
    return sorted(f.path.as_posix() for g in r.actionable for f in g.to_remove)


A = rec("/d/a.txt", mtime=100)
B = rec("/d/b.txt", mtime=200)
C = rec("/d/c.txt", mtime=300)


def test_keep_newest_and_oldest():
    assert keep_of([A, B, C], [Rule(K.KEEP_NEWEST)]) == "/d/c.txt"
    assert keep_of([A, B, C], [Rule(K.KEEP_OLDEST)]) == "/d/a.txt"


def test_ctime_option():
    x, y = rec("/d/x", mtime=1, ctime=900), rec("/d/y", mtime=9, ctime=1)
    assert keep_of([x, y], [Rule(K.KEEP_NEWEST, {"by": "ctime"})]) == "/d/x"
    assert keep_of([x, y], [Rule(K.KEEP_NEWEST)]) == "/d/y"


def test_shortest_path_and_name():
    short, long_ = rec("/d/a.txt"), rec("/d/sub/folder/a_long_name.txt")
    assert keep_of([long_, short], [Rule(K.KEEP_SHORTEST_PATH)]) == "/d/a.txt"
    x, y = rec("/aaaaaaaaaa/z.txt"), rec("/b/zzzzzzzz.txt")
    assert keep_of([x, y], [Rule(K.KEEP_SHORTEST_PATH, {"by": "name"})]) == "/aaaaaaaaaa/z.txt"


def test_prefer_folder_and_avoid():
    inside, outside = rec("/photos/a.jpg"), rec("/downloads/a.jpg")
    assert keep_of([outside, inside], [Rule(K.PREFER_FOLDER, {"folder": "/photos"})]) == "/photos/a.jpg"
    assert keep_of([outside, inside], [Rule(K.PREFER_FOLDER, {"folder": "/downloads", "avoid": True})]) == "/photos/a.jpg"


def test_folder_match_is_not_a_string_prefix():
    near = rec("/data/photos2/a.jpg", mtime=1)
    real = rec("/data/photos/a.jpg", mtime=2)
    # "/data/photos2" must NOT count as inside "/data/photos"
    assert keep_of([near, real], [Rule(K.PREFER_FOLDER, {"folder": "/data/photos"})]) == "/data/photos/a.jpg"
    assert removed([near, real], [Rule(K.PROTECT_FOLDER, {"folder": "/data/photos"})]) == ["/data/photos2/a.jpg"]


def test_rule_chain_breaks_ties_in_priority_order():
    p_old = rec("/pics/old.jpg", mtime=1)
    p_new = rec("/pics/new.jpg", mtime=5)
    other = rec("/tmp/newest.jpg", mtime=9)
    rules = [Rule(K.PREFER_FOLDER, {"folder": "/pics"}), Rule(K.KEEP_NEWEST)]
    assert keep_of([other, p_old, p_new], rules) == "/pics/new.jpg"


def test_priority_number_overrides_list_order():
    rules = [Rule(K.KEEP_OLDEST, priority=2), Rule(K.KEEP_NEWEST, priority=1)]
    assert keep_of([A, B, C], rules) == "/d/c.txt"


def test_no_rules_is_deterministic_oldest_first():
    assert keep_of([C, B, A], []) == "/d/a.txt"
    t1, t2 = rec("/d/zz/same.txt", mtime=5), rec("/d/same.txt", mtime=5)
    assert keep_of([t1, t2], []) == "/d/same.txt"  # tie -> shorter path


def test_protect_folder_never_removed_and_keep_comes_from_protected():
    safe, tmp = rec("/safe/a.txt", mtime=999), rec("/tmp/a.txt", mtime=1)
    rules = [Rule(K.PROTECT_FOLDER, {"folder": "/safe"}), Rule(K.KEEP_OLDEST)]
    assert keep_of([safe, tmp], rules) == "/safe/a.txt"  # oldest rule would pick /tmp, protection wins
    assert removed([safe, tmp], rules) == ["/tmp/a.txt"]


def test_multiple_protected_files_all_survive():
    s1, s2, t = rec("/safe/1.txt"), rec("/safe/2.txt"), rec("/tmp/3.txt")
    assert removed([s1, s2, t], [Rule(K.PROTECT_FOLDER, {"folder": "/safe"})]) == ["/tmp/3.txt"]


def test_fully_protected_group_is_untouched():
    r = apply_rules([group(rec("/safe/a"), rec("/safe/sub/b"))], [Rule(K.PROTECT_FOLDER, {"folder": "/safe"})])
    assert r.actionable == [] and len(r.untouched) == 1


def test_filter_extensions_protects_other_types():
    jpg, png = rec("/d/a.JPG"), rec("/d/b.png")
    rules = [Rule(K.FILTER, {"extensions": ["jpg"]})]
    assert removed([jpg, png], rules) == ["/d/a.JPG"]  # png is protected, so only jpg may go


def test_filter_modified_before_iso_date():
    old = rec("/d/old.txt", mtime=datetime(2020, 1, 1).timestamp())
    new = rec("/d/new.txt", mtime=datetime(2025, 1, 1).timestamp())
    rules = [Rule(K.FILTER, {"modified_before": "2023-01-01"}), Rule(K.KEEP_NEWEST)]
    assert removed([old, new], rules) == ["/d/old.txt"]
    rules = [Rule(K.FILTER, {"modified_after": "2023-01-01"})]
    assert removed([old, new], rules) == ["/d/new.txt"]


def test_filter_name_and_size_conditions():
    f1, f2 = rec("/d/copy of a.txt", size=10), rec("/d/a.txt", size=10)
    assert removed([f1, f2], [Rule(K.FILTER, {"name_contains": "COPY"})]) == ["/d/copy of a.txt"]
    assert removed([f1, f2], [Rule(K.FILTER, {"name_regex": r"^copy"})]) == ["/d/copy of a.txt"]
    assert removed([f1, f2], [Rule(K.FILTER, {"min_size": 50})]) == []  # too small: all protected
    assert removed([f1, f2], [Rule(K.FILTER, {"max_size": 50})]) != []


def test_multiple_filters_must_all_match():
    f1, f2 = rec("/d/x.jpg"), rec("/d/y.jpg")
    rules = [Rule(K.FILTER, {"extensions": ["jpg"]}), Rule(K.FILTER, {"name_contains": "x"})]
    assert removed([f1, f2], rules) == ["/d/x.jpg"]


def test_yaml_style_date_objects_accepted():
    import datetime as dt
    old = rec("/d/old.txt", mtime=datetime(2020, 1, 1).timestamp())
    new = rec("/d/new.txt", mtime=datetime(2025, 1, 1).timestamp())
    assert removed([old, new], [Rule(K.FILTER, {"modified_before": dt.date(2023, 1, 1)})]) == ["/d/old.txt"]


def test_totals():
    g1 = group(rec("/a/1", size=1000), rec("/b/1", size=1000), rec("/c/1", size=1000))
    g2 = group(rec("/a/2", size=50), rec("/b/2", size=50))
    r = apply_rules([g1, g2], [])
    assert r.total_to_remove == 3 and r.bytes_to_recover == 2050


def test_input_groups_not_mutated():
    g = group(A, B)
    apply_rules([g], [Rule(K.PROTECT_FOLDER, {"folder": "/d"})])
    assert g.keep is None and g.protected == []


@pytest.mark.parametrize("rule", [
    Rule(K.KEEP_NEWEST, {"by": "atime"}),
    Rule(K.KEEP_NEWEST, {"bogus": 1}),
    Rule(K.KEEP_SHORTEST_PATH, {"by": "size"}),
    Rule(K.PREFER_FOLDER, {}),
    Rule(K.PROTECT_FOLDER, {"folder": ""}),
    Rule(K.FILTER, {}),
    Rule(K.FILTER, {"nope": 1}),
    Rule(K.FILTER, {"modified_before": "not a date"}),
    Rule(K.FILTER, {"name_regex": "("}),
])
def test_invalid_rules_raise_readable_errors(rule):
    with pytest.raises(ValueError):
        validate_rules([rule])


def test_safety_invariant_holds_for_random_inputs():
    rnd = random.Random(42)
    folders = ["/a", "/b", "/c/d"]
    for _ in range(300):
        files = [rec(f"{rnd.choice(folders)}/f{i}.{rnd.choice(['jpg','png'])}",
                     mtime=rnd.randint(1, 50), ctime=rnd.randint(1, 50)) for i in range(rnd.randint(2, 6))]
        pool = [Rule(K.KEEP_NEWEST), Rule(K.KEEP_OLDEST), Rule(K.KEEP_SHORTEST_PATH),
                Rule(K.PREFER_FOLDER, {"folder": rnd.choice(folders), "avoid": rnd.random() < .5}),
                Rule(K.PROTECT_FOLDER, {"folder": rnd.choice(folders)}),
                Rule(K.FILTER, {"extensions": [rnd.choice(["jpg", "png"])]})]
        rules = [r for r in pool if rnd.random() < .5]
        res = apply_rules([group(*files)], rules)
        for g in res.actionable + res.untouched:
            assert g.keep in g.files
            assert g.keep not in g.to_remove
            assert not set(g.to_remove) & set(g.protected)
            assert len(g.to_remove) < len(g.files)  # at least one copy always survives
