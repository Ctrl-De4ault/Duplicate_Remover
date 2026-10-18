# DupRemover: Phased Implementation Plan

A tool that finds duplicate files by **content** (not by name or location) and removes them in a fully customizable way, in one click.

## Core Idea

Files may have different names and live in different folders, so duplicates are detected by content hash. Users define rules for which copy to keep and what happens to the rest. Saved profiles make cleanup a one-click action.

## Detection Strategy

1. **Group by size.** Files of different sizes cannot be duplicates.
2. **Partial hash.** Hash the first few KB of each remaining file to cheaply rule out most non-matches.
3. **Full hash.** Use SHA-256 or BLAKE3 on the survivors. Matching hashes mean identical content.

## Customization Options

- **Keep rules:** oldest, newest, shortest path or name
- **Folder rules:** preferred folders, protected folders that are never touched
- **Filters:** file type, size range, date
- **Actions for non-kept copies:** recycle bin, quarantine folder, hardlink/symlink replacement, permanent delete

## Phases

### Phase 0: Decisions and Setup
- Choose language and interface (CLI or GUI)
- Create the repo
- Define the core data model: `File`, `DuplicateGroup`, `Rule`, `Action`

**Output:** project skeleton and agreed data model.

### Phase 1: Scanner
- Walk chosen folders recursively
- Collect path, size, and modified/created time
- Handle symlinks, permission errors, and excluded folders

**Output:** a list of file records.

### Phase 2: Duplicate Detection Engine
- Size grouping, then partial hash, then full hash
- Parallel processing
- Hash cache so rescans are fast

**Output:** duplicate groups. This is the heart of the project and gets the heaviest testing.

### Phase 3: Rule Engine
- Keep rules: newest/oldest, shortest path, preferred or protected folders, file type, size, date filters
- Rules are combinable and ordered by priority

**Output:** for every group, one "keep" file and the rest marked for action.

### Phase 4: Action Executor and Safety
- Recycle bin, quarantine folder, hardlink/symlink replacement, permanent delete
- Dry-run mode
- Safety check so a group never loses all of its copies
- Undo log

**Output:** safe, reversible execution of the chosen action.

### Phase 5: Profiles and One-Click
- Save folders, rules, and action as named profiles (JSON or YAML), e.g. "Photos cleanup", "Downloads cleanup"
- One click or one command runs a profile
- First run of any profile requires a dry-run preview

**Output:** one-click cleanup from saved profiles.

### Phase 6: Interface
- Results view
- Rule builder
- Preview of what will be deleted
- Progress display

**Output:** CLI or GUI layered on top of the finished engine.

### Phase 7: Testing, Polish, Packaging
- Edge cases: huge files, unicode names, locked files
- Performance tuning
- Package as an installer or executable

**Output:** a releasable build.

## Safety Principles

- Default to recycle bin or quarantine, never permanent delete
- Dry-run preview before the first real run of any profile
- Never delete every copy in a group
- Keep an undo log

## Open Decision

**Language:** Python (fastest to build, easy GUI with PyQt or Tkinter) versus Rust or Go (faster scanning, single binary).

Phases 1 to 5 form the core engine and are independent of the interface choice.

## Similar Existing Tools

dupeGuru, fdupes, rmlint, Czkawka. Most are rigid or have clunky rule systems, so a clean, flexible rule engine is the gap this project fills.
