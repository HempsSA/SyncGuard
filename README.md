# SyncGuard

**FreeFileSync Job Manager** — a dark-mode desktop GUI for managing, scheduling, and safely running multiple FreeFileSync sync jobs with automatic change-rate detection and abort protection.

![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

## Why SyncGuard?

FreeFileSync is powerful, but running multiple sync jobs manually is tedious and risky. SyncGuard adds a safety layer: before every sync, it performs a full parallel rescan of your source folder, compares the change rate against a configurable threshold, and **blocks the sync if too many files changed** — preventing accidental mass overwrites.

## Features

- **Multi-job management** — Create, duplicate, remove, and run multiple FreeFileSync jobs from one interface
- **Automatic scheduling** — Set daily run times per job with a live countdown
- **Change-rate protection** — Configurable threshold (default 40%) with an override dialog when exceeded
- **Parallel multi-threaded scanning** — Uses Windows `FindFirstFileExW` with `LARGE_FETCH` for maximum speed; auto-tunes worker count for local vs network paths
- **Warm cache** — File/directory mtimes are cached between runs so subsequent scans skip unchanged directories entirely
- **Folder Guardian** — Real-time rename-deny and delete-logging via watchdog, with auto-pause when FreeFileSync is running
- **Scan history** — Last 200 runs per job with CSV export
- **System tray** — Minimizes to tray with status-colored icon; right-click to run all or quit
- **Dark mode** — GitHub-dark-inspired UI with responsive scaling for different screen sizes

## Quick Start

### Prerequisites

- Python 3.8+
- [FreeFileSync](https://freefilesync.org) installed

### Install & Run

```bash
# Clone the repository
git clone https://github.com/HempsSA/SyncGuard.git
cd SyncGuard

# Run directly
python syncguard_protected.py

# Or as a package
python -m syncguard
```

Dependencies (`customtkinter`, `schedule`, `pystray`, `Pillow`, `watchdog`, `psutil`) are **auto-installed on first run**.

## Usage

1. **Add a job** — Click "+ New Job" in the top bar
2. **Configure** — Set the source folder, FreeFileSync executable, batch file, and log file paths
3. **Set threshold** — Adjust the change-rate limit (default: 40%). If more files changed than this percentage, the sync is blocked and you're asked to confirm
4. **Schedule (optional)** — Go to the Schedule tab and add run times in `HH:MM` format
5. **Run** — Click "Run Now" or let the scheduler trigger it automatically

### Folder Guardian

The Guardian tab lets you watch a folder in real-time:
- **Renames are blocked** — if anything renames a file, SyncGuard immediately moves it back
- **Deletions are logged** — the `.guardian_lockfile` is restored if deleted; all other deletions are logged for FreeFileSync to handle
- **Auto-pause** — optionally pauses protection while FreeFileSync is running (requires `psutil`)

## Architecture

```
syncguard/
├── __init__.py        # Package version
├── __main__.py        # Entry point, dependency bootstrap
├── constants.py       # Paths, colour palette, diagnostics
├── persistence.py     # Atomic JSON writes, corruption recovery, data models
├── scanner.py         # Windows-native dir listing, parallel scanner, change guard
├── guardian.py        # Watchdog-based rename/del protection
└── app.py             # CustomTkinter GUI, scheduler, tray integration
```

## Configuration

SyncGuard stores its data alongside the script:

| File | Purpose |
|------|---------|
| `syncguard_jobs.json` | Job configurations (auto-created, with `.bak` backup) |
| `syncguard_cache/` | Per-job scan caches and history (auto-created) |

Both use atomic writes with backup — if the JSON becomes corrupt, SyncGuard recovers from the `.bak` file on next startup.

## License

MIT
