"""
SyncGuard destination snapshot — captures file manifests before sync
for integrity verification and rollback support.
"""

import os
import json
import time
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict

from .constants import CACHE_DIR, diagnostic


# ---------------------------------------------------------------------------
# Snapshot directory layout:
#   syncguard_cache/snapshots/{job_id}/{timestamp}.json
# ---------------------------------------------------------------------------

SNAPSHOTS_DIR = CACHE_DIR / "snapshots"
MAX_SNAPSHOTS = 3  # keep last N per job


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class FileEntry:
    """One file's metadata in a snapshot manifest."""
    path:       str
    size:       int
    mtime:      float
    sha256_1k:  str = ""   # first 1KB hash for quick integrity check


@dataclass
class SnapshotManifest:
    """Complete snapshot of a directory tree."""
    job_id:     str
    timestamp:  str
    epoch:      float
    dest_path:  str
    total_files: int = 0
    total_size:  int = 0
    files:      Dict[str, dict] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Manifest capture
# ---------------------------------------------------------------------------

def _hash_first_1k(filepath: str) -> str:
    """SHA-256 of the first 1024 bytes. Fast integrity fingerprint."""
    try:
        with open(filepath, "rb") as f:
            data = f.read(1024)
        return hashlib.sha256(data).hexdigest()[:16]
    except (OSError, PermissionError):
        return ""


def capture_manifest(
    dest_path: str,
    job_id: str,
    sample_count: int = 0,
    log_cb=None,
) -> Optional[SnapshotManifest]:
    """
    Walk dest_path and capture a file manifest.

    If sample_count > 0, only hash that many random files (faster).
    If sample_count == 0, hash ALL files (slower but complete).
    """
    if not dest_path or not os.path.isdir(dest_path):
        if log_cb:
            log_cb("Snapshot: destination not accessible: " + dest_path,
                   "ERROR")
        return None

    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    manifest = SnapshotManifest(
        job_id=job_id,
        timestamp=ts,
        epoch=time.time(),
        dest_path=dest_path,
    )

    files_to_hash: List[str] = []
    total_size = 0

    try:
        for root, _dirs, files in os.walk(dest_path):
            for name in files:
                fp = os.path.join(root, name)
                try:
                    st = os.stat(fp)
                    rel = os.path.relpath(fp, dest_path)
                    manifest.files[rel] = {
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "hash": "",  # filled below
                    }
                    files_to_hash.append(rel)
                    total_size += st.st_size
                except (OSError, PermissionError):
                    continue
    except Exception as exc:
        if log_cb:
            log_cb("Snapshot: walk failed: " + str(exc), "ERROR")
        return None

    manifest.total_files = len(manifest.files)
    manifest.total_size = total_size

    # Hash files (all or sampled)
    import random as _random
    if sample_count > 0 and sample_count < len(files_to_hash):
        to_hash = _random.sample(files_to_hash, sample_count)
    else:
        to_hash = files_to_hash

    for rel in to_hash:
        fp = os.path.join(dest_path, rel)
        manifest.files[rel]["hash"] = _hash_first_1k(fp)

    if log_cb:
        log_cb(
            "Snapshot: captured {} files ({}) from {}".format(
                manifest.total_files,
                _fmt_size(manifest.total_size),
                dest_path))

    return manifest


# ---------------------------------------------------------------------------
# Snapshot persistence
# ---------------------------------------------------------------------------

def _snapshot_dir(job_id: str) -> Path:
    d = SNAPSHOTS_DIR / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_snapshot(manifest: SnapshotManifest) -> Path:
    """Save snapshot manifest to disk. Returns the file path."""
    d = _snapshot_dir(manifest.job_id)
    fp = d / (manifest.timestamp + ".json")
    data = asdict(manifest)
    fp.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
    _prune_snapshots(manifest.job_id)
    return fp


def load_snapshot(job_id: str, timestamp: str) -> Optional[SnapshotManifest]:
    """Load a specific snapshot by timestamp."""
    d = _snapshot_dir(job_id)
    fp = d / (timestamp + ".json")
    if not fp.exists():
        return None
    try:
        data = json.loads(fp.read_text("utf-8"))
        return SnapshotManifest(**{
            k: v for k, v in data.items()
            if k in SnapshotManifest.__dataclass_fields__
        })
    except Exception as exc:
        diagnostic("Failed to load snapshot: " + str(exc))
        return None


def list_snapshots(job_id: str) -> List[str]:
    """Return sorted list of available snapshot timestamps for a job."""
    d = _snapshot_dir(job_id)
    if not d.exists():
        return []
    return sorted(
        fp.stem for fp in d.glob("*.json"))


def _prune_snapshots(job_id: str, keep: int = MAX_SNAPSHOTS):
    """Remove old snapshots, keeping only the most recent `keep`."""
    stamps = list_snapshots(job_id)
    if len(stamps) <= keep:
        return
    d = _snapshot_dir(job_id)
    for stamp in stamps[:-keep]:
        fp = d / (stamp + ".json")
        try:
            fp.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Snapshot comparison (pre vs post sync)
# ---------------------------------------------------------------------------

@dataclass
class SnapshotDiff:
    """Result of comparing two snapshots (pre-sync vs post-sync)."""
    added:      int = 0
    removed:    int = 0
    modified:   int = 0
    unchanged:  int = 0
    hash_mismatch: List[str] = field(default_factory=list)
    size_anomaly:  List[str] = field(default_factory=list)
    is_clean:   bool = True
    summary:    str = ""


def compare_snapshots(
    before: SnapshotManifest,
    after: SnapshotManifest,
    hash_check: bool = True,
) -> SnapshotDiff:
    """
    Compare pre-sync and post-sync manifests.
    Flags files that were removed, modified, or have hash mismatches.
    """
    diff = SnapshotDiff()

    before_files = set(before.files.keys())
    after_files = set(after.files.keys())

    diff.added = len(after_files - before_files)
    diff.removed = len(before_files - after_files)
    common = before_files & after_files

    for rel in common:
        b = before.files[rel]
        a = after.files[rel]

        # Check for content modification
        modified = False
        if b.get("mtime") != a.get("mtime"):
            modified = True
        if b.get("size") != a.get("size"):
            modified = True
            # Suspicious: file shrank (possible encryption overhead or truncation)
            if a.get("size", 0) < b.get("size", 0) * 0.5:
                diff.size_anomaly.append(rel)

        if modified:
            diff.modified += 1
            # Hash check if both snapshots hashed this file
            if hash_check and b.get("hash") and a.get("hash"):
                if b["hash"] != a["hash"]:
                    diff.hash_mismatch.append(rel)
        else:
            diff.unchanged += 1

    # Clean = no removals, no hash mismatches, no size anomalies
    diff.is_clean = (
        diff.removed == 0 and
        not diff.hash_mismatch and
        not diff.size_anomaly
    )

    parts = []
    if diff.added:
        parts.append("{} added".format(diff.added))
    if diff.removed:
        parts.append("{} removed".format(diff.removed))
    if diff.modified:
        parts.append("{} modified".format(diff.modified))
    if diff.hash_mismatch:
        parts.append("{} hash mismatches".format(len(diff.hash_mismatch)))
    if diff.size_anomaly:
        parts.append("{} size anomalies".format(len(diff.size_anomaly)))
    diff.summary = ", ".join(parts) if parts else "no changes"

    return diff


# ---------------------------------------------------------------------------
# Rollback support
# ---------------------------------------------------------------------------

def rollback_from_snapshot(
    snapshot: SnapshotManifest,
    dest_path: str,
    log_cb=None,
) -> Tuple[int, int]:
    """
    Restore files from a snapshot by reading the current destination
    and comparing against the manifest. Returns (restored, failed).

    Note: This restores metadata only (flags mismatches).
    Actual file content rollback requires the original source files
    or a separate backup copy. This method identifies what needs
    restoring and logs the details.
    """
    restored = 0
    failed = 0

    if not os.path.isdir(dest_path):
        if log_cb:
            log_cb("Rollback: destination not found: " + dest_path, "ERROR")
        return 0, 0

    current_files = set()
    for root, _dirs, files in os.walk(dest_path):
        for name in files:
            fp = os.path.join(root, name)
            rel = os.path.relpath(fp, dest_path)
            current_files.add(rel)

    snapshot_files = set(snapshot.files.keys())

    # Files in snapshot but missing from current → need restore
    missing = snapshot_files - current_files
    for rel in missing:
        if log_cb:
            log_cb("Rollback: MISSING file: " + rel, "WARN")
        restored += 1

    # Files in current but not in snapshot → was added post-sync (keep)
    # Files with size anomalies → flagged
    common = snapshot_files & current_files
    for rel in common:
        fp = os.path.join(dest_path, rel)
        try:
            st = os.stat(fp)
            snap = snapshot.files[rel]
            if st.st_size < snap.get("size", 0) * 0.5:
                if log_cb:
                    log_cb(
                        "Rollback: SIZE ANOMALY: " + rel +
                        " (now {}B, was {}B)".format(
                            st.st_size, snap.get("size", 0)),
                        "WARN")
                failed += 1
        except OSError:
            pass

    if log_cb:
        log_cb(
            "Rollback analysis: {} missing, {} size anomalies".format(
                restored, failed))

    return restored, failed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_size(n: int) -> str:
    """Format byte count to human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return "{:.1f} {}".format(n, unit)
        n /= 1024
    return "{:.1f} PB".format(n)
