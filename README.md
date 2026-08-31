# SyncGuard

**FreeFileSync Job Manager** — a dark-mode desktop GUI for managing, scheduling, and safely running multiple FreeFileSync sync jobs with automatic change-rate detection, ransomware protection, and abort protection.

![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

## Why SyncGuard?

FreeFileSync is powerful, but running multiple sync jobs manually is tedious and risky. SyncGuard adds a safety layer: before every sync, it performs a full parallel rescan of your source folder, compares the change rate against a configurable threshold, and **blocks the sync if too many files changed** — preventing accidental mass overwrites or ransomware propagation.

## Features

### Core
- **Multi-job management** — Create, duplicate, remove, and run multiple FreeFileSync jobs from one interface
- **Automatic scheduling** — Set daily run times per job with a live countdown
- **Change-rate protection** — Configurable threshold (default 40%) with an override dialog when exceeded
- **Parallel multi-threaded scanning** — Uses Windows `FindFirstFileExW` with `LARGE_FETCH` for maximum speed; auto-tunes worker count for local vs network paths
- **Warm cache** — File/directory mtimes are cached between runs so subsequent scans skip unchanged directories entirely
- **Scan history** — Last 200 runs per job with CSV export
- **System tray** — Minimizes to tray with status-colored icon; right-click to run all or quit
- **Dark mode** — GitHub-dark-inspired UI with responsive scaling for different screen sizes

### Folder Guardian
- **Real-time rename-deny** — Blocks any file renames and immediately reverts them
- **Delete logging** — Logs all deletions; `.guardian_lockfile` is auto-restored if deleted
- **Auto-pause** — Optionally pauses protection while FreeFileSync is running (requires `psutil`)

### Ransomware Protection
- **Entropy analysis** — Samples random changed files and checks Shannon entropy; encrypted files have entropy near 8.0 bits/byte
- **Extension detection** — 223+ known ransomware extensions (WannaCry, Locky, Dharma, Conti, LockBit, Hive, Akira, STOP/Djvu, and more)
- **Anomaly scoring** — Weighted composite score combining change rate, entropy, extensions, deletion ratio, and rename ratio; blocks sync when score exceeds threshold
- **Pre-sync snapshots** — Captures destination file manifest with SHA-256 hashes before each sync
- **Post-sync validation** — Compares pre/post snapshots to detect hash mismatches and size anomalies
- **Destination rollback** — CLI tool to list snapshots and analyze what needs restoring

## Quick Start

### Windows Installer (Recommended)

Download or clone the repo, then run:

```
setup.bat
```

This will:
1. Check for Git and Python
2. Clone the repository to `C:\SyncGuard` (or a custom location)
3. Install all Python dependencies
4. Optionally launch SyncGuard

```
setup.bat D:\MyFolder\SyncGuard    # custom install location
```

### Manual Install

```bash
# Clone the repository
git clone https://github.com/HempsSA/SyncGuard.git
cd SyncGuard

# Install dependencies
pip install -r requirements.txt

# Run (no console window)
pythonw SyncGuard.pyw

# Or with console (for debugging)
python syncguard_protected.py

# Or as a package
python -m syncguard
```

Dependencies (`customtkinter`, `schedule`, `pystray`, `Pillow`, `watchdog`, `psutil`) are **auto-installed on first run**.

> **Tip:** Double-click `SyncGuard.pyw` to launch without a black terminal window. Use `syncguard_protected.py` if you need console output for debugging.

## Usage

1. **Add a job** — Click "+ New Job" in the top bar
2. **Configure** — Set the source folder, FreeFileSync executable, batch file, and log file paths
3. **Set threshold** — Adjust the change-rate limit (default: 40%). If more files changed than this percentage, the sync is blocked and you're asked to confirm
4. **Enable ransomware protection** — Go to the Ransomware tab:
   - Set the **Destination Path** (where FreeFileSync writes to)
   - Enable/disable protection, adjust entropy threshold and block score
   - Review or customize the suspicious extensions list
5. **Schedule (optional)** — Go to the Schedule tab and add run times in `HH:MM` format
6. **Run** — Click "Run Now" or let the scheduler trigger it automatically

### Folder Guardian

The Guardian tab lets you watch a folder in real-time:
- **Renames are blocked** — if anything renames a file, SyncGuard immediately moves it back
- **Deletions are logged** — the `.guardian_lockfile` is restored if deleted; all other deletions are logged for FreeFileSync to handle
- **Auto-pause** — optionally pauses protection while FreeFileSync is running (requires `psutil`)

### Ransomware Protection

The Ransomware tab provides multi-layered protection:

| Check | What it detects | How |
|-------|----------------|-----|
| **Entropy analysis** | Encrypted file content | Shannon entropy >7.5 bits/byte |
| **Extension detection** | Ransomware naming patterns | 223+ known extensions |
| **Anomaly score** | Composite threat level | Weighted: change + entropy + extensions + deletes + renames |
| **Pre-sync snapshot** | Destination integrity | SHA-256 hashes of destination files |
| **Post-sync validation** | Corruption after sync | Hash mismatches and size anomalies |

**Anomaly Score Weights:**
- Change rate: 40% (0–40 points)
- High entropy: 25 points (binary: detected or not)
- Suspicious extensions: 20 points (binary: detected or not)
- Mass deletions: 0–15 points (proportional)
- Mass renames: 0–10 points (proportional)

Default block threshold: **60/100**. If the score exceeds this, the sync is blocked.

### Rollback CLI

List available snapshots:
```bash
python -m syncguard.rollback --list --job "Job 1"
```

Analyze the latest snapshot:
```bash
python -m syncguard.rollback --job "Job 1" --latest
```

Interactive selection:
```bash
python -m syncguard.rollback --job "Job 1"
```

## Architecture

```
syncguard/
├── __init__.py        # Package version
├── __main__.py        # Entry point, dependency bootstrap
├── constants.py       # Paths, colour palette, diagnostics
├── persistence.py     # Atomic JSON writes, corruption recovery, data models
├── scanner.py         # Windows-native dir listing, parallel scanner, change guard
├── guardian.py        # Watchdog-based rename/del protection
├── ransomware.py      # Entropy sampling, extension detection, anomaly scoring
├── snapshot.py        # Destination manifest capture and comparison
├── rollback.py        # CLI for destination rollback
└── app.py             # CustomTkinter GUI, scheduler, tray integration
```

## Configuration

SyncGuard stores its data alongside the script:

| File | Purpose |
|------|---------|
| `syncguard_jobs.json` | Job configurations (auto-created, with `.bak` backup) |
| `syncguard_cache/` | Per-job scan caches, history, and snapshots (auto-created) |
| `syncguard_cache/snapshots/` | Pre-sync destination manifests (auto-pruned to last 3) |

Both use atomic writes with backup — if the JSON becomes corrupt, SyncGuard recovers from the `.bak` file on next startup.

### Job Configuration Fields

| Field | Default | Description |
|-------|---------|-------------|
| `source_path` | — | Source folder to scan |
| `destination_path` | — | Destination folder (for snapshot validation) |
| `threshold` | 40 | Max % change rate before blocking |
| `hours_back` | 24 | Time window for change detection |
| `num_workers` | 0 (auto) | Scan threads (0 = auto: 4 local / 8 network) |
| `ransomware_protection` | true | Enable entropy + extension + anomaly checks |
| `entropy_threshold` | 7.5 | Shannon entropy threshold (encrypted ≈ 8.0) |
| `snapshot_before_sync` | true | Capture destination manifest before sync |
| `anomaly_block_score` | 60 | Score above which sync is blocked |
| `custom_extensions` | [] | Additional suspicious extensions (empty = use defaults) |
| `exclude_patterns` | [] | Glob patterns to skip during scan |
| `schedule_times` | [] | Daily run times in `HH:MM` format |
| `guardian_folder` | — | Folder to watch for renames/deletions |
| `guardian_auto_pause` | false | Pause guardian while FFS is running |

## License

MIT
