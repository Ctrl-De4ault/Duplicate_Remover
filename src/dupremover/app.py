"""Phase 6: pywebview launcher and the Python API the UI calls."""
from __future__ import annotations

import json
import threading
from datetime import datetime
from dataclasses import replace
from pathlib import Path

from .actions import execute, undo
from .models import ActionKind
from .profiles import (ProfileError, ProfileStore, build_plan, fingerprint, profile_from_dict,
                       profile_to_dict, run_profile)

UI_DIR = Path(__file__).resolve().parent / "ui"
UNDOABLE = {ActionKind.QUARANTINE, ActionKind.HARDLINK, ActionKind.SYMLINK}


def _plain(obj):
    return json.loads(json.dumps(obj, default=str))


class Api:
    """Public methods are callable from JS. State is underscore-prefixed so pywebview ignores it."""

    def __init__(self, store: ProfileStore | None = None):
        self._store = store or ProfileStore()
        self._window = None
        self._groups, self._skipped, self._profile = [], set(), None
        self._p = {"stage": "idle", "done": 0, "total": 0}
        self._busy, self._error, self._finished = False, None, None
        self._cancel, self._report, self._log = False, None, None

    # ---- background work
    def _spawn(self, kind, fn):
        self._busy, self._error, self._finished, self._cancel = True, None, None, False
        self._p = {"stage": "scan", "done": 0, "total": 0}

        def work():
            try:
                fn()
            except Exception as e:
                self._error = f"{type(e).__name__}: {e}"
            finally:
                self._busy, self._finished = False, kind

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def _progress(self, stage, done, total):
        self._p = {"stage": stage, "done": done, "total": total}

    def _parse(self, d):
        try:
            return profile_from_dict(d), None
        except ProfileError as e:
            return None, str(e)

    def status(self):
        return {**self._p, "busy": self._busy, "error": self._error, "finished": self._finished}

    def cancel(self):
        self._cancel = True

    # ---- profiles
    def profiles(self):
        good, broken = self._store.list()
        return {"profiles": _plain([profile_to_dict(p) for p in good]), "broken": len(broken)}

    def save_profile(self, d):
        p, err = self._parse(d)
        if err:
            return {"error": err}
        try:
            self._store.save(p)
        except ProfileError as e:
            return {"error": str(e)}
        return {"ok": True}

    def delete_profile(self, name):
        self._store.delete(name)
        return {"ok": True}

    def pick_folders(self):
        import webview
        kind = webview.FileDialog.FOLDER if hasattr(webview, "FileDialog") else webview.FOLDER_DIALOG
        return list(self._window.create_file_dialog(kind, allow_multiple=True) or [])

    # ---- scan and review
    def start_scan(self, d):
        p, err = self._parse(d)
        if err:
            return {"error": err}
        self._profile, self._groups, self._skipped = p, [], set()

        def work():
            plan = build_plan(p, on_progress=self._progress)
            self._groups = list(plan.rules.actionable)

        return self._spawn("scan", work)

    def _summary(self):
        live = [g for i, g in enumerate(self._groups) if i not in self._skipped]
        return {"groups": len(self._groups), "files": sum(len(g.to_remove) for g in live),
                "bytes": sum(g.size * len(g.to_remove) for g in live)}

    def _ser(self, i):
        g = self._groups[i]
        prot = set(g.protected)
        return {"id": i, "size": g.size, "skipped": i in self._skipped, "files": [
            {"id": j, "name": f.path.name, "folder": str(f.path.parent), "path": str(f.path),
             "date": datetime.fromtimestamp(f.mtime).strftime("%d %b %Y"),
             "keep": f == g.keep, "protected": f in prot} for j, f in enumerate(g.files)]}

    def results(self, offset=0, limit=50):
        end = min(offset + limit, len(self._groups))
        return {"groups": [self._ser(i) for i in range(offset, end)], "total": len(self._groups),
                "summary": self._summary()}

    def set_keep(self, gid, fid):
        g = self._groups[gid]
        self._groups[gid] = replace(g, keep=g.files[fid])
        return {"group": self._ser(gid), "summary": self._summary()}

    def skip_group(self, gid, skipped):
        (self._skipped.add if skipped else self._skipped.discard)(gid)
        return {"summary": self._summary()}

    # ---- run, result, undo
    def _confirm_saved(self, p):
        try:
            if fingerprint(self._store.load(p.name)) == fingerprint(p):
                self._store.confirm(p.name)  # reviewed and run once: one-click is now allowed
        except ProfileError:
            pass

    def run(self, confirm_delete=False):
        p = self._profile
        groups = [g for i, g in enumerate(self._groups) if i not in self._skipped]
        if not groups:
            return {"error": "Nothing selected to clean up."}
        if p.action is ActionKind.DELETE and not confirm_delete:
            return {"error": "Permanent delete needs confirmation."}

        def work():
            rep = execute(groups, p.action, dry_run=False, quarantine_dir=p.quarantine_dir,
                          confirm_permanent=confirm_delete,
                          on_progress=lambda d, t: self._progress("execute", d, t),
                          should_cancel=lambda: self._cancel)
            self._report, self._log = rep, rep.log_path
            self._confirm_saved(p)

        return self._spawn("run", work)

    def quick_run(self, d, confirm_delete=False):
        p, err = self._parse(d)
        if err:
            return {"error": err}
        if not p.dry_run_confirmed:
            return {"error": "Scan and review this profile once before using Run now."}
        if p.action is ActionKind.DELETE and not confirm_delete:
            return {"error": "Permanent delete needs confirmation."}
        self._profile = p

        def work():
            res = run_profile(p, dry_run=False, confirm_permanent=confirm_delete,
                              on_progress=self._progress, should_cancel=lambda: self._cancel)
            self._report, self._log = res.report, res.report.log_path

        return self._spawn("run", work)

    def run_result(self):
        r = self._report
        note = {ActionKind.RECYCLE: "restore from the Recycle Bin if needed",
                ActionKind.DELETE: "permanently deleted"}.get(r.action, "")
        return {"done": len(r.done), "skipped": len(r.skipped), "failed": len(r.failed),
                "bytes": r.bytes_recovered, "cancelled": r.cancelled, "note": note,
                "can_undo": r.action in UNDOABLE and bool(r.done)}

    def undo_last(self):
        if not self._log:
            return {"error": "Nothing to undo."}
        u = undo(self._log)
        return {"restored": len(u.restored), "skipped": len(u.skipped), "failed": len(u.failed)}


def main() -> None:
    import webview
    api = Api()
    api._window = webview.create_window(
        "DupRemover", str(UI_DIR / "index.html"), js_api=api,
        width=1120, height=760, min_size=(900, 620))
    webview.start()


if __name__ == "__main__":
    main()
