"""Phase 5: saved profiles and the one-click run.

A profile is a YAML file holding the folders, scan filters, rules and action.

    name: Photos cleanup
    folders:
      - 'E:\\Photos'
    exclude_names: [.git]
    extensions: [jpg, png]
    rules:
      - kind: prefer_folder
        folder: 'E:\\Photos\\Originals'
      - kind: keep_newest
    action: quarantine

Safety: a profile's first real run needs a preview. `preview_confirmed` in the
file stores a fingerprint of the profile's settings; it only counts while the
settings still match, so ANY change to folders, filters, rules or action (even
a hand edit) requires a new preview. Renaming does not.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from .actions import ExecutionReport, default_data_dir, execute
from .detector import DetectionResult, find_duplicates
from .models import ActionKind, Profile, Rule, RuleKind
from .rules import RuleResult, apply_rules, validate_rules
from .scanner import ScanResult, scan

ProgressFn = Callable[[str, int, int], None]  # (stage, done, total); stage: scan|partial|full|execute

_TOP_KEYS = {"name", "folders", "excludes", "exclude_names", "min_size", "max_size",
             "extensions", "rules", "action", "quarantine_dir", "preview_confirmed"}


class ProfileError(ValueError):
    """A profile is malformed. The message says what to fix."""


class PreviewRequired(Exception):
    """A real run was requested for a profile whose current settings were never previewed."""


# ------------------------------------------------------------ (de)serialising

def _plain(v):
    if isinstance(v, Path):
        return str(v)
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


def _rule_to_dict(r: Rule) -> dict:
    d: dict = {"kind": r.kind.value}
    if r.priority:
        d["priority"] = r.priority
    d.update(_plain(r.params))
    return d


def _settings(p: Profile) -> dict:
    return {
        "folders": [str(x) for x in p.folders],
        "excludes": [str(x) for x in p.excludes],
        "exclude_names": list(p.exclude_names),
        "min_size": p.min_size,
        "max_size": p.max_size,
        "extensions": list(p.extensions) if p.extensions is not None else None,
        "rules": [_rule_to_dict(r) for r in p.rules],
        "action": p.action.value,
        "quarantine_dir": str(p.quarantine_dir) if p.quarantine_dir else None,
    }


def fingerprint(p: Profile) -> str:
    blob = json.dumps(_settings(p), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def profile_to_dict(p: Profile) -> dict:
    s = _settings(p)
    d: dict = {"name": p.name, "folders": s["folders"]}
    for key in ("excludes", "exclude_names"):
        if s[key]:
            d[key] = s[key]
    d["min_size"] = s["min_size"]
    for key in ("max_size", "extensions"):
        if s[key] is not None:
            d[key] = s[key]
    d["rules"] = s["rules"]
    d["action"] = s["action"]
    if s["quarantine_dir"]:
        d["quarantine_dir"] = s["quarantine_dir"]
    if p.dry_run_confirmed:
        d["preview_confirmed"] = fingerprint(p)
    return d


def _str_list(d: dict, key: str, required: bool = False) -> list[str] | None:
    if key not in d or d[key] is None:
        if required:
            raise ProfileError(f"'{key}' is required")
        return None
    v = d[key]
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise ProfileError(f"'{key}' must be a list of text values")
    return v


def _int(d: dict, key: str, default, minimum: int = 0):
    if key not in d or d[key] is None:
        return default
    v = d[key]
    if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
        raise ProfileError(f"'{key}' must be a whole number >= {minimum}")
    return v


def _rule_from_dict(i: int, raw) -> Rule:
    if not isinstance(raw, dict) or "kind" not in raw:
        raise ProfileError(f"rule #{i}: each rule needs a 'kind'")
    params = dict(raw)
    kind_name = params.pop("kind")
    try:
        kind = RuleKind(kind_name)
    except ValueError:
        options = ", ".join(k.value for k in RuleKind)
        raise ProfileError(f"rule #{i}: unknown kind {kind_name!r} (choose from: {options})") from None
    priority = params.pop("priority", 0)
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise ProfileError(f"rule #{i}: 'priority' must be a whole number")
    if isinstance(params.get("folder"), str):
        params["folder"] = str(Path(params["folder"]).expanduser())
    return Rule(kind=kind, params=params, priority=priority)


def profile_from_dict(d) -> Profile:
    if not isinstance(d, dict):
        raise ProfileError("a profile must be a set of 'key: value' settings")
    unknown = set(d) - _TOP_KEYS
    if unknown:
        raise ProfileError(f"unknown setting(s): {', '.join(sorted(map(str, unknown)))}")
    name = d.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ProfileError("'name' is required")
    folders = [Path(x).expanduser() for x in _str_list(d, "folders", required=True)]
    if not folders:
        raise ProfileError("'folders' needs at least one folder")
    excludes = [Path(x).expanduser() for x in (_str_list(d, "excludes") or [])]
    min_size = _int(d, "min_size", 1)
    max_size = _int(d, "max_size", None)
    if max_size is not None and max_size < min_size:
        raise ProfileError("'max_size' cannot be smaller than 'min_size'")
    action_name = d.get("action", ActionKind.RECYCLE.value)
    try:
        action = ActionKind(action_name)
    except ValueError:
        options = ", ".join(a.value for a in ActionKind)
        raise ProfileError(f"unknown action {action_name!r} (choose from: {options})") from None
    raw_rules = d.get("rules") or []
    if not isinstance(raw_rules, list):
        raise ProfileError("'rules' must be a list")
    rules = [_rule_from_dict(i, r) for i, r in enumerate(raw_rules, 1)]
    try:
        validate_rules(rules)
    except ValueError as e:
        raise ProfileError(f"rules: {e}") from None
    quarantine = d.get("quarantine_dir")
    if quarantine is not None and not isinstance(quarantine, str):
        raise ProfileError("'quarantine_dir' must be a folder path")
    profile = Profile(
        name=name.strip(), folders=folders, excludes=excludes, rules=rules, action=action,
        exclude_names=_str_list(d, "exclude_names") or [], min_size=min_size, max_size=max_size,
        extensions=_str_list(d, "extensions"),
        quarantine_dir=Path(quarantine).expanduser() if quarantine else None,
    )
    stored = d.get("preview_confirmed")
    profile.dry_run_confirmed = bool(stored) and stored == fingerprint(profile)
    return profile


def save_profile_file(profile: Profile, path: Path) -> None:
    """Atomic write: a crash can never leave a half-written profile."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        yaml.safe_dump(profile_to_dict(profile), sort_keys=False, allow_unicode=True,
                       default_flow_style=False),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def load_profile_file(path: Path) -> Profile:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ProfileError(f"not valid YAML ({e}). Tip: wrap Windows paths in single quotes") from None
    return profile_from_dict(data)


# ----------------------------------------------------------------------- store

def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "profile"


class ProfileStore:
    """Profiles as YAML files in one folder (default ~/.dupremover/profiles)."""

    def __init__(self, directory: str | Path | None = None):
        self.dir = Path(directory) if directory else default_data_dir() / "profiles"

    def path_for(self, name: str) -> Path:
        return self.dir / f"{slugify(name)}.yaml"

    def save(self, profile: Profile) -> Path:
        path = self.path_for(profile.name)
        if path.exists():
            try:
                existing = yaml.safe_load(path.read_text(encoding="utf-8"))
                other = existing.get("name") if isinstance(existing, dict) else None
            except (yaml.YAMLError, OSError):
                other = None
            if other and other != profile.name:
                raise ProfileError(f"a different profile named {other!r} already uses that file name")
        save_profile_file(profile, path)
        return path

    def load(self, name: str) -> Profile:
        path = self.path_for(name)
        if not path.exists():
            raise ProfileError(f"no profile named {name!r}")
        return load_profile_file(path)

    def list(self) -> tuple[list[Profile], list[tuple[Path, str]]]:
        """Return (valid profiles, [(broken file, reason)])."""
        good: list[Profile] = []
        broken: list[tuple[Path, str]] = []
        for path in sorted(self.dir.glob("*.yaml")) if self.dir.exists() else []:
            try:
                good.append(load_profile_file(path))
            except (ProfileError, OSError) as e:
                broken.append((path, str(e)))
        return good, broken

    def delete(self, name: str) -> None:
        self.path_for(name).unlink(missing_ok=True)

    def confirm(self, name: str) -> Profile:
        """Mark the profile's CURRENT settings as previewed."""
        profile = self.load(name)
        profile.dry_run_confirmed = True
        self.save(profile)
        return profile


# ---------------------------------------------------------------------- running

@dataclass
class Plan:
    scan: ScanResult
    detection: DetectionResult
    rules: RuleResult


@dataclass
class RunResult:
    plan: Plan
    report: ExecutionReport


def build_plan(profile: Profile, *, use_cache: bool = True, cache_path: str | Path | None = None,
               on_progress: ProgressFn | None = None) -> Plan:
    """Scan, find duplicates and apply the profile's rules. Touches no files."""
    progress_scan = (lambda n: on_progress("scan", n, 0)) if on_progress else None
    scanned = scan(
        profile.folders, excludes=profile.excludes, exclude_names=profile.exclude_names,
        min_size=profile.min_size, max_size=profile.max_size, extensions=profile.extensions,
        on_progress=progress_scan,
    )
    cache = None
    if use_cache:
        cache = Path(cache_path) if cache_path else default_data_dir() / "hashcache.sqlite"
        cache.parent.mkdir(parents=True, exist_ok=True)
    detection = find_duplicates(scanned.files, cache_path=cache, on_progress=on_progress)
    return Plan(scanned, detection, apply_rules(detection.groups, profile.rules))


def run_profile(
    profile: Profile,
    *,
    dry_run: bool | None = None,
    confirm_permanent: bool = False,
    log_dir: str | Path | None = None,
    use_cache: bool = True,
    cache_path: str | Path | None = None,
    on_progress: ProgressFn | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> RunResult:
    """The one-click run.

    dry_run=None (default): a real run if the profile's current settings were
    previewed, otherwise a preview. dry_run=False on an unpreviewed profile
    raises PreviewRequired.
    """
    if dry_run is None:
        dry_run = not profile.dry_run_confirmed
    if not dry_run:
        if not profile.dry_run_confirmed:
            raise PreviewRequired(
                f"'{profile.name}' has not been previewed with its current settings. "
                "Run a preview first, then confirm it."
            )
        if profile.action is ActionKind.DELETE and not confirm_permanent:
            raise ValueError("this profile permanently deletes files; pass confirm_permanent=True")
    plan = build_plan(profile, use_cache=use_cache, cache_path=cache_path, on_progress=on_progress)
    report = execute(
        plan.rules.actionable, profile.action, dry_run=dry_run,
        quarantine_dir=profile.quarantine_dir, log_dir=log_dir, confirm_permanent=confirm_permanent,
        on_progress=(lambda d, t: on_progress("execute", d, t)) if on_progress else None,
        should_cancel=should_cancel,
    )
    return RunResult(plan, report)
