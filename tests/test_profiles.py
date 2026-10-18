import datetime as dt
import os

import pytest
import yaml

from dupremover import profiles
from dupremover.actions import undo
from dupremover.models import ActionKind as A, Profile, Rule, RuleKind as K
from dupremover.profiles import (
    PreviewRequired, ProfileError, ProfileStore, build_plan, fingerprint, load_profile_file,
    profile_from_dict, run_profile, save_profile_file, slugify,
)

CONTENT = b"payload " * 100


def sample(tmp_path, **kw):
    base = dict(
        name="Photos cleanup", folders=[tmp_path / "data"], excludes=[tmp_path / "data" / "skip"],
        rules=[Rule(K.PREFER_FOLDER, {"folder": str(tmp_path / "data" / "a")}),
               Rule(K.KEEP_NEWEST, priority=2),
               Rule(K.FILTER, {"extensions": ["jpg"], "modified_before": "2030-01-01"})],
        action=A.QUARANTINE, exclude_names=[".git"], min_size=10, max_size=10_000_000,
        extensions=["jpg", "png"], quarantine_dir=tmp_path / "q",
    )
    base.update(kw)
    return Profile(**base)


def make_files(tmp_path, spec):
    root = tmp_path / "data"
    for rel, (data, mtime) in spec.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        os.utime(p, (mtime, mtime))
    return root


SPEC = {"a/keep.jpg": (CONTENT, 100), "b/copy1.jpg": (CONTENT, 200), "c/copy2.jpg": (CONTENT, 300)}


def run(profile, tmp_path, **kw):
    return run_profile(profile, cache_path=tmp_path / "cache.db", log_dir=tmp_path / "logs", **kw)


# ------------------------------------------------------------ file format

def test_roundtrip_preserves_everything(tmp_path):
    p = sample(tmp_path)
    f = tmp_path / "p.yaml"
    save_profile_file(p, f)
    q = load_profile_file(f)
    assert q == p


def test_file_is_human_readable(tmp_path):
    f = tmp_path / "p.yaml"
    save_profile_file(sample(tmp_path), f)
    text = f.read_text()
    assert "kind: prefer_folder" in text and "action: quarantine" in text and "name: Photos cleanup" in text
    assert not list(tmp_path.glob("*.tmp"))


def test_minimal_profile_gets_safe_defaults():
    p = profile_from_dict({"name": "x", "folders": ["/data"]})
    assert p.action is A.RECYCLE and p.min_size == 1 and p.rules == [] and not p.dry_run_confirmed


def test_unquoted_yaml_dates_work(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("name: d\nfolders: ['/x']\nrules:\n  - kind: filter\n    modified_before: 2023-01-01\n")
    p = load_profile_file(f)
    assert isinstance(p.rules[0].params["modified_before"], dt.date)
    save_profile_file(p, tmp_path / "again.yaml")
    assert load_profile_file(tmp_path / "again.yaml") == p


def test_tilde_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    p = profile_from_dict({"name": "x", "folders": ["~/pics"]})
    assert p.folders == [tmp_path / "pics"]


@pytest.mark.parametrize("data, fragment", [
    ("just text", "key: value"),
    ({"folders": ["/x"]}, "'name'"),
    ({"name": "x"}, "'folders'"),
    ({"name": "x", "folders": []}, "at least one"),
    ({"name": "x", "folders": "/x", "bogus": 1}, "unknown setting"),
    ({"name": "x", "folders": ["/x"], "action": "shred"}, "unknown action"),
    ({"name": "x", "folders": ["/x"], "min_size": -1}, "min_size"),
    ({"name": "x", "folders": ["/x"], "min_size": 10, "max_size": 5}, "max_size"),
    ({"name": "x", "folders": ["/x"], "rules": "keep_newest"}, "list"),
    ({"name": "x", "folders": ["/x"], "rules": [{"folder": "/y"}]}, "kind"),
    ({"name": "x", "folders": ["/x"], "rules": [{"kind": "keep_best"}]}, "unknown kind"),
    ({"name": "x", "folders": ["/x"], "rules": [{"kind": "keep_newest", "by": "atime"}]}, "rules:"),
    ({"name": "x", "folders": ["/x"], "rules": [{"kind": "filter"}]}, "rules:"),
])
def test_invalid_profiles_give_readable_errors(data, fragment):
    with pytest.raises(ProfileError) as e:
        profile_from_dict(data)
    assert fragment in str(e.value)


def test_broken_yaml_file(tmp_path):
    f = tmp_path / "bad.yaml"
    f.write_text('name: "E:\\Photos"\n')  # invalid escape in double quotes
    with pytest.raises(ProfileError):
        load_profile_file(f)


# --------------------------------------------------- preview confirmation

def test_confirmation_survives_save_and_load(tmp_path):
    p = sample(tmp_path)
    p.dry_run_confirmed = True
    f = tmp_path / "p.yaml"
    save_profile_file(p, f)
    assert load_profile_file(f).dry_run_confirmed is True


@pytest.mark.parametrize("edit", [
    lambda d: d.update(action="delete"),
    lambda d: d["folders"].append("/another"),
    lambda d: d["rules"].append({"kind": "keep_oldest"}),
    lambda d: d.update(min_size=5),
    lambda d: d.update(extensions=["gif"]),
])
def test_any_settings_change_invalidates_confirmation(tmp_path, edit):
    p = sample(tmp_path)
    p.dry_run_confirmed = True
    f = tmp_path / "p.yaml"
    save_profile_file(p, f)
    data = yaml.safe_load(f.read_text())
    edit(data)
    f.write_text(yaml.safe_dump(data, sort_keys=False))  # hand edit keeps the old fingerprint
    assert load_profile_file(f).dry_run_confirmed is False


def test_rename_keeps_confirmation(tmp_path):
    p = sample(tmp_path)
    fp = fingerprint(p)
    p.name = "Renamed"
    assert fingerprint(p) == fp


# ------------------------------------------------------------------- store

def test_store_save_load_list_delete(tmp_path):
    store = ProfileStore(tmp_path / "profiles")
    path = store.save(sample(tmp_path))
    assert path.name == "photos-cleanup.yaml"
    assert store.load("Photos cleanup").name == "Photos cleanup"
    (store.dir / "broken.yaml").write_text("name: [oops")
    good, broken = store.list()
    assert [g.name for g in good] == ["Photos cleanup"] and broken[0][0].name == "broken.yaml"
    store.delete("Photos cleanup")
    with pytest.raises(ProfileError):
        store.load("Photos cleanup")


def test_store_refuses_to_overwrite_a_different_profile(tmp_path):
    store = ProfileStore(tmp_path / "profiles")
    store.save(sample(tmp_path, name="Photos!"))
    with pytest.raises(ProfileError):
        store.save(sample(tmp_path, name="photos"))  # same file name, different profile
    store.save(sample(tmp_path, name="Photos!"))  # updating the same profile is fine


def test_store_confirm(tmp_path):
    store = ProfileStore(tmp_path / "profiles")
    store.save(sample(tmp_path))
    assert store.load("Photos cleanup").dry_run_confirmed is False
    store.confirm("Photos cleanup")
    assert store.load("Photos cleanup").dry_run_confirmed is True


def test_slugify():
    assert slugify("Photos cleanup!") == "photos-cleanup" and slugify("???") == "profile"


# ------------------------------------------------------------ running it

def one_click(tmp_path, **kw):
    make_files(tmp_path, SPEC)
    return sample(tmp_path, rules=[Rule(K.KEEP_OLDEST)], excludes=[], **kw)


def test_unconfirmed_profile_defaults_to_preview(tmp_path):
    p = one_click(tmp_path)
    res = run(p, tmp_path)
    assert res.report.dry_run and len(res.report.planned) == 2
    assert (tmp_path / "data/b/copy1.jpg").exists()


def test_real_run_without_preview_is_refused(tmp_path):
    p = one_click(tmp_path)
    with pytest.raises(PreviewRequired):
        run(p, tmp_path, dry_run=False)
    assert (tmp_path / "data/b/copy1.jpg").exists()


def test_full_flow_preview_confirm_run_undo(tmp_path):
    store = ProfileStore(tmp_path / "profiles")
    store.save(one_click(tmp_path))
    assert run(store.load("Photos cleanup"), tmp_path).report.dry_run
    p = store.confirm("Photos cleanup")
    res = run(p, tmp_path)  # one click: now a real run
    assert not res.report.dry_run and len(res.report.done) == 2
    assert (tmp_path / "data/a/keep.jpg").exists() and not (tmp_path / "data/b/copy1.jpg").exists()
    assert len(undo(res.report.log_path).restored) == 2
    assert (tmp_path / "data/b/copy1.jpg").read_bytes() == CONTENT


def test_editing_after_confirmation_forces_a_new_preview(tmp_path):
    store = ProfileStore(tmp_path / "profiles")
    store.save(one_click(tmp_path))
    p = store.confirm("Photos cleanup")
    p.rules.append(Rule(K.KEEP_NEWEST))  # edit through the normal API
    p.dry_run_confirmed = False
    store.save(p)
    assert run(store.load("Photos cleanup"), tmp_path).report.dry_run


def test_permanent_delete_refused_before_any_scanning(tmp_path, monkeypatch):
    p = one_click(tmp_path, action=A.DELETE)
    p.dry_run_confirmed = True
    monkeypatch.setattr(profiles, "scan", lambda *a, **k: pytest.fail("should not scan"))
    with pytest.raises(ValueError):
        run(p, tmp_path, dry_run=False)


def test_plan_honours_profile_filters(tmp_path):
    make_files(tmp_path, {
        "a/x.jpg": (CONTENT, 1), "b/x.jpg": (CONTENT, 2),          # duplicate jpgs: found
        "a/y.txt": (CONTENT + b"1", 1), "b/y.txt": (CONTENT + b"1", 2),   # wrong extension
        ".git/z.jpg": (CONTENT + b"2", 1), "g/z.jpg": (CONTENT + b"2", 2),  # .git excluded
        "a/tiny.jpg": (b"abc", 1), "b/tiny.jpg": (b"abc", 2),      # below min_size
    })
    p = sample(tmp_path, rules=[], excludes=[], min_size=50)
    plan = build_plan(p, use_cache=False)
    assert [sorted(f.path.name for f in g.files) for g in plan.detection.groups] == [["x.jpg", "x.jpg"]]


def test_excluded_folders_are_not_scanned(tmp_path):
    make_files(tmp_path, {"a/x.jpg": (CONTENT, 1), "skip/x.jpg": (CONTENT, 2)})
    p = sample(tmp_path, rules=[], excludes=[tmp_path / "data" / "skip"])
    assert build_plan(p, use_cache=False).detection.groups == []


def test_progress_reports_every_stage(tmp_path):
    p = one_click(tmp_path)
    stages = []
    run(p, tmp_path, on_progress=lambda s, d, t: stages.append(s))
    assert {"scan", "execute"} <= set(stages)
