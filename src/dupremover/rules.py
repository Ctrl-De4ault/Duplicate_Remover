"""Phase 3: decide which copy to keep in each duplicate group.

Rule kinds
----------
Ranking rules pick the copy to keep. They are applied in priority order
(lower `priority` first, ties by list order); a later rule only breaks ties
left by earlier ones.
    KEEP_NEWEST          params: by = "mtime" (default) | "ctime"
    KEEP_OLDEST          params: by = "mtime" (default) | "ctime"
    KEEP_SHORTEST_PATH   params: by = "path" (default) | "name"
    PREFER_FOLDER        params: folder (required), avoid (bool, default False)
                         keeps the copy inside `folder`; with avoid=True, outside it

Safety rules never remove a file:
    PROTECT_FOLDER       params: folder (required). Files under it are never removed.
    FILTER               params (at least one): extensions, min_size, max_size,
                         modified_before, modified_after, name_contains, name_regex.
                         Only files matching ALL conditions may be removed;
                         non-matching files are protected. Several FILTER rules
                         must ALL match.

Implicit final tie-breakers (so results are deterministic): oldest modified
time, then shortest path, then alphabetical path.

Invariant: every group keeps at least one file, `keep` is never in
`to_remove`, and protected files are never in `to_remove`.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time

from .models import DuplicateGroup, FileRecord, Rule, RuleKind

_RANKING = {RuleKind.KEEP_NEWEST, RuleKind.KEEP_OLDEST, RuleKind.KEEP_SHORTEST_PATH, RuleKind.PREFER_FOLDER}
_FILTER_KEYS = {"extensions", "min_size", "max_size", "modified_before", "modified_after",
                "name_contains", "name_regex"}


@dataclass
class RuleResult:
    actionable: list[DuplicateGroup] = field(default_factory=list)  # something to remove
    untouched: list[DuplicateGroup] = field(default_factory=list)   # fully protected, nothing to remove

    @property
    def total_to_remove(self) -> int:
        return sum(len(g.to_remove) for g in self.actionable)

    @property
    def bytes_to_recover(self) -> int:
        return sum(g.size * len(g.to_remove) for g in self.actionable)


def _norm(p) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def _under(path, folder_norm: str) -> bool:
    a = _norm(path)
    return a == folder_norm or a.startswith(folder_norm.rstrip("\\/") + os.sep)


def _check_keys(rule: Rule, allowed: set[str]) -> None:
    unknown = set(rule.params) - allowed
    if unknown:
        raise ValueError(f"{rule.kind.value}: unknown parameter(s) {sorted(unknown)}")


def _choice(rule: Rule, key: str, options: tuple[str, ...]) -> str:
    value = rule.params.get(key, options[0])
    if value not in options:
        raise ValueError(f"{rule.kind.value}: '{key}' must be one of {list(options)}, got {value!r}")
    return value


def _folder(rule: Rule) -> str:
    if not rule.params.get("folder"):
        raise ValueError(f"{rule.kind.value}: 'folder' is required")
    return _norm(rule.params["folder"])


def _to_ts(value, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"filter: '{name}' must be a date or timestamp")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, date):  # YAML turns unquoted 2024-01-01 into a date
        return datetime.combine(value, time.min).timestamp()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            pass
    raise ValueError(f"filter: '{name}' must be an ISO date like '2024-01-31' or a timestamp, got {value!r}")


def _build_filter(rule: Rule) -> Callable[[FileRecord], bool]:
    p = rule.params
    _check_keys(rule, _FILTER_KEYS)
    if not p:
        raise ValueError("filter: at least one condition is required")
    exts = None
    if "extensions" in p:
        raw = [p["extensions"]] if isinstance(p["extensions"], str) else list(p["extensions"])
        exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in raw}
    before = _to_ts(p["modified_before"], "modified_before") if "modified_before" in p else None
    after = _to_ts(p["modified_after"], "modified_after") if "modified_after" in p else None
    contains = p["name_contains"].lower() if "name_contains" in p else None
    try:
        regex = re.compile(p["name_regex"], re.IGNORECASE) if "name_regex" in p else None
    except re.error as e:
        raise ValueError(f"filter: invalid name_regex ({e})") from e
    lo, hi = p.get("min_size"), p.get("max_size")

    def matches(f: FileRecord) -> bool:
        name = f.path.name
        return not (
            (exts is not None and f.path.suffix.lower() not in exts)
            or (lo is not None and f.size < lo)
            or (hi is not None and f.size > hi)
            or (before is not None and f.mtime >= before)
            or (after is not None and f.mtime <= after)
            or (contains is not None and contains not in name.lower())
            or (regex is not None and not regex.search(name))
        )

    return matches


def _build_rank_part(rule: Rule) -> Callable[[FileRecord], object]:
    kind = rule.kind
    if kind in (RuleKind.KEEP_NEWEST, RuleKind.KEEP_OLDEST):
        _check_keys(rule, {"by"})
        attr = _choice(rule, "by", ("mtime", "ctime"))
        sign = -1 if kind is RuleKind.KEEP_NEWEST else 1
        return lambda f: sign * getattr(f, attr)
    if kind is RuleKind.KEEP_SHORTEST_PATH:
        _check_keys(rule, {"by"})
        by = _choice(rule, "by", ("path", "name"))
        return (lambda f: len(f.path.name)) if by == "name" else (lambda f: len(str(f.path)))
    _check_keys(rule, {"folder", "avoid"})  # PREFER_FOLDER
    folder = _folder(rule)
    avoid = bool(rule.params.get("avoid", False))
    return lambda f: int(_under(f.path, folder)) if avoid else int(not _under(f.path, folder))


@dataclass
class _Prepared:
    protect_folders: list[str]
    filters: list[Callable[[FileRecord], bool]]
    rank_parts: list[Callable[[FileRecord], object]]


def _prepare(rules: list[Rule]) -> _Prepared:
    ordered = sorted(enumerate(rules), key=lambda t: (t[1].priority, t[0]))  # stable
    prep = _Prepared([], [], [])
    for _, rule in ordered:
        if rule.kind is RuleKind.PROTECT_FOLDER:
            _check_keys(rule, {"folder"})
            prep.protect_folders.append(_folder(rule))
        elif rule.kind is RuleKind.FILTER:
            prep.filters.append(_build_filter(rule))
        elif rule.kind in _RANKING:
            prep.rank_parts.append(_build_rank_part(rule))
    return prep


def validate_rules(rules: list[Rule]) -> None:
    """Raise ValueError with a readable message if any rule is malformed."""
    _prepare(rules)


def apply_rules(groups: list[DuplicateGroup], rules: list[Rule]) -> RuleResult:
    """Choose `keep` and `protected` for every group. Input groups are not modified."""
    prep = _prepare(rules)

    def rank(f: FileRecord) -> tuple:
        s = str(f.path)
        return (*(part(f) for part in prep.rank_parts), f.mtime, len(s), s.lower(), s)

    result = RuleResult()
    for g in groups:
        if not g.files:
            continue
        protected = [
            f for f in g.files
            if any(_under(f.path, d) for d in prep.protect_folders)
            or not all(flt(f) for flt in prep.filters)
        ]
        # A protected file is guaranteed to survive, so keep is chosen among
        # protected files first; otherwise among all files.
        keep = min(protected or g.files, key=rank)
        new = replace(g, keep=keep, protected=protected)
        (result.actionable if new.to_remove else result.untouched).append(new)
    return result
