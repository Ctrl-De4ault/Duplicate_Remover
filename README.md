# DupRemover

Finds duplicate files by **content**, not by name or location, and cleans them up using rules you control. Save a setup as a profile and run it again in one click.

## Features

- Detects duplicates by content (size, then partial hash, then full hash), so renamed files and files in different folders are still found
- Hardlinks to the same file are recognised and never counted as duplicates
- Choose which copy to keep: oldest, newest, shortest path, preferred folder
- Protect folders and restrict cleanup by file type, size, date or name
- Actions: Recycle Bin, quarantine folder, hardlink, symlink, or permanent delete
- Saved profiles (plain YAML) with a one-click "Run now"
- Single-window desktop interface that follows the system light or dark theme

## Safety

- Nothing is removed until you review the results and click Clean up
- Before touching a file, DupRemover re-checks it is unchanged and byte-for-byte identical to the kept copy
- At least one copy of every file always survives
- Every action is logged, and quarantine, hardlink and symlink runs can be undone
- A profile's one-click run only works after its current settings were reviewed once; any change requires a new review
- Permanent delete asks for explicit confirmation

Try it on a test folder first.

## Install

**Windows:** download `DupRemover-windows.zip` from the [Releases](../../releases) page, extract it, and run `DupRemover.exe`. The app is unsigned, so Windows SmartScreen may show a warning the first time (More info, then Run anyway). It needs the Microsoft Edge WebView2 runtime, which is included with Windows 11 and most up-to-date Windows 10 installs.

**From source (Python 3.11+):**

```
pip install -e .
python -m dupremover
```

## Profiles

Profiles live in `~/.dupremover/profiles` as YAML files:

```yaml
name: Photos cleanup
folders:
  - 'E:\Photos'
extensions: [jpg, png]
rules:
  - kind: protect_folder
    folder: 'E:\Photos\Originals'
  - kind: keep_newest
action: quarantine
```

Rule kinds: `keep_newest`, `keep_oldest`, `keep_shortest_path`, `prefer_folder`, `protect_folder`, `filter`. Actions: `recycle`, `quarantine`, `hardlink`, `symlink`, `delete`. Wrap Windows paths in single quotes. The rule engine is documented in `src/dupremover/rules.py`.

## Development

```
pip install -e ".[dev]"
pytest
ruff check .
```

Build the Windows app locally:

```
pip install -e ".[build]"
pyinstaller DupRemover.spec
```

Pushing a tag such as `v0.1.0` builds the app on GitHub Actions and attaches `DupRemover-windows.zip` to a release.

```
src/dupremover/
  scanner.py    walk folders, collect file metadata
  detector.py   find duplicates by content, with a hash cache
  rules.py      choose which copy to keep
  actions.py    carry out the plan safely, with undo log
  profiles.py   YAML profiles and the one-click run
  app.py        desktop window and the API the UI calls
  ui/           HTML, CSS and JS interface
tests/
docs/PLAN.md    original phased plan
```

## Status

Early release. Developed and tested mainly on Windows; the interface has not been tested on macOS or Linux. No image previews yet.

## License

MIT
