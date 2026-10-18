import os

import pytest

from dupremover.scanner import scan


def _write(p, data=b"x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def names(result):
    return sorted(f.path.name for f in result.files)


def test_finds_files_recursively(tmp_path):
    _write(tmp_path / "a.txt")
    _write(tmp_path / "sub" / "b.txt")
    _write(tmp_path / "sub" / "deep" / "c.txt")
    assert names(scan([tmp_path])) == ["a.txt", "b.txt", "c.txt"]


def test_record_has_metadata(tmp_path):
    _write(tmp_path / "a.txt", b"hello")
    f = scan([tmp_path]).files[0]
    assert f.size == 5 and f.mtime > 0
    assert f.inode != 0


def test_skips_empty_files_by_default(tmp_path):
    _write(tmp_path / "empty.txt", b"")
    _write(tmp_path / "full.txt", b"x")
    r = scan([tmp_path])
    assert names(r) == ["full.txt"] and r.skipped_small == 1
    assert names(scan([tmp_path], min_size=0)) == ["empty.txt", "full.txt"]


def test_max_size(tmp_path):
    _write(tmp_path / "small.bin", b"x" * 10)
    _write(tmp_path / "big.bin", b"x" * 1000)
    assert names(scan([tmp_path], max_size=100)) == ["small.bin"]


def test_extension_filter(tmp_path):
    _write(tmp_path / "a.JPG")
    _write(tmp_path / "b.png")
    _write(tmp_path / "c.txt")
    assert names(scan([tmp_path], extensions=["jpg", ".png"])) == ["a.JPG", "b.png"]


def test_excluded_folder_path(tmp_path):
    _write(tmp_path / "keep" / "a.txt")
    _write(tmp_path / "skip" / "b.txt")
    assert names(scan([tmp_path], excludes=[tmp_path / "skip"])) == ["a.txt"]


def test_excluded_folder_name(tmp_path):
    _write(tmp_path / "a.txt")
    _write(tmp_path / "x" / ".git" / "obj.txt")
    assert names(scan([tmp_path], exclude_names=[".git"])) == ["a.txt"]


def test_overlapping_roots_no_double_count(tmp_path):
    _write(tmp_path / "sub" / "a.txt")
    r = scan([tmp_path, tmp_path / "sub", tmp_path])
    assert names(r) == ["a.txt"]


def test_missing_root_recorded_as_error(tmp_path):
    r = scan([tmp_path / "nope"])
    assert r.files == [] and len(r.errors) == 1


def test_symlinks_skipped(tmp_path):
    target = _write(tmp_path / "real.txt")
    try:
        os.symlink(target, tmp_path / "link.txt")
        os.symlink(tmp_path, tmp_path / "loop", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")
    r = scan([tmp_path])
    assert names(r) == ["real.txt"] and r.skipped_symlinks == 2


def test_hardlinks_share_identity(tmp_path):
    a = _write(tmp_path / "a.txt", b"data")
    try:
        os.link(a, tmp_path / "b.txt")
    except OSError:
        pytest.skip("hardlinks not permitted here")
    fa, fb = sorted(scan([tmp_path]).files, key=lambda f: f.path.name)
    assert (fa.dev, fa.inode) == (fb.dev, fb.inode)


def test_progress_callback(tmp_path):
    for i in range(3):
        _write(tmp_path / f"{i}.txt")
    seen = []
    scan([tmp_path], on_progress=seen.append)
    assert seen == [1, 2, 3]
