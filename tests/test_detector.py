import os
from pathlib import Path

import pytest

from dupremover import detector
from dupremover.detector import find_duplicates
from dupremover.scanner import scan


def _w(p: Path, data: bytes) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def run(tmp_path, **kw):
    return find_duplicates(scan([tmp_path]).files, **kw)


def group_names(result):
    return sorted(sorted(f.path.name for f in g.files) for g in result.groups)


def test_same_content_different_names_and_folders(tmp_path):
    _w(tmp_path / "a" / "photo.jpg", b"same content")
    _w(tmp_path / "b" / "holiday.jpg", b"same content")
    _w(tmp_path / "c" / "unrelated.txt", b"different!!!!")
    assert group_names(run(tmp_path)) == [["holiday.jpg", "photo.jpg"]]


def test_unique_files_not_reported(tmp_path):
    _w(tmp_path / "a.txt", b"one")
    _w(tmp_path / "b.txt", b"two")
    assert run(tmp_path).groups == []


def test_same_size_different_content_not_grouped(tmp_path):
    _w(tmp_path / "a.bin", b"A" * 50)
    _w(tmp_path / "b.bin", b"B" * 50)
    assert run(tmp_path).groups == []


def test_large_files_differing_only_in_the_middle(tmp_path):
    head, tail = b"H" * 4096, b"T" * 4096
    _w(tmp_path / "a.bin", head + b"x" * 100_000 + tail)
    _w(tmp_path / "b.bin", head + b"y" * 100_000 + tail)  # same head/tail, different middle
    _w(tmp_path / "c.bin", head + b"x" * 100_000 + tail)  # true duplicate of a
    assert group_names(run(tmp_path)) == [["a.bin", "c.bin"]]


def test_hashes_are_populated(tmp_path):
    _w(tmp_path / "a.bin", b"z" * 50_000)
    _w(tmp_path / "b.bin", b"z" * 50_000)
    g = run(tmp_path).groups[0]
    assert all(f.full_hash == g.hash and f.partial_hash for f in g.files)


def test_hardlinks_are_not_duplicates(tmp_path):
    a = _w(tmp_path / "a.txt", b"data")
    try:
        os.link(a, tmp_path / "b.txt")
    except OSError:
        pytest.skip("hardlinks not permitted here")
    r = run(tmp_path)
    assert r.groups == [] and r.hardlinks_collapsed == 1


def test_groups_sorted_by_wasted_space(tmp_path):
    _w(tmp_path / "s1.bin", b"s" * 10); _w(tmp_path / "s2.bin", b"s" * 10)
    _w(tmp_path / "b1.bin", b"b" * 9000); _w(tmp_path / "b2.bin", b"b" * 9000)
    r = run(tmp_path)
    assert [g.size for g in r.groups] == [9000, 10]
    assert r.total_wasted_bytes == 9010


def test_wasted_bytes_counts_all_extra_copies(tmp_path):
    for n in "abc":
        _w(tmp_path / f"{n}.bin", b"q" * 100)
    assert run(tmp_path).groups[0].wasted_bytes == 200


def test_file_vanishing_after_scan_is_reported_not_fatal(tmp_path):
    _w(tmp_path / "a.bin", b"k" * 100)
    gone = _w(tmp_path / "b.bin", b"k" * 100)
    _w(tmp_path / "c.bin", b"k" * 100)
    files = scan([tmp_path]).files
    gone.unlink()
    r = find_duplicates(files)
    assert [p.name for p, _ in r.errors] == ["b.bin"]
    assert group_names(r) == [["a.bin", "c.bin"]]


def test_progress_callback_reports_stages(tmp_path):
    _w(tmp_path / "a.bin", b"p" * 20_000); _w(tmp_path / "b.bin", b"p" * 20_000)
    events = []
    run(tmp_path, on_progress=lambda s, d, t: events.append((s, d, t)))
    assert {e[0] for e in events} == {"partial", "full"}
    assert events[-1][1] == events[-1][2]


def test_cache_skips_rehashing_and_invalidates_on_change(tmp_path, monkeypatch):
    a = _w(tmp_path / "a.bin", b"c" * 30_000)
    _w(tmp_path / "b.bin", b"c" * 30_000)
    db = tmp_path.parent / (tmp_path.name + "_cache.db")
    first = run(tmp_path, cache_path=db)

    calls = []
    real_full, real_partial = detector._full_hash, detector._partial_hash
    monkeypatch.setattr(detector, "_full_hash", lambda p, s: calls.append(p) or real_full(p, s))
    monkeypatch.setattr(detector, "_partial_hash", lambda p, s: calls.append(p) or real_partial(p, s))

    second = run(tmp_path, cache_path=db)
    assert calls == [] and group_names(second) == group_names(first)

    a.write_bytes(b"d" * 30_000)  # modify: size same, mtime changes
    os.utime(a, (a.stat().st_atime, a.stat().st_mtime + 10))
    third = run(tmp_path, cache_path=db)
    assert calls and third.groups == []
