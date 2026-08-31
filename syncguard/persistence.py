"""
SyncGuard persistence — atomic JSON writes, corruption recovery,
and data models (JobConfig, JobStore, ScanHistory, ScanCache).
"""

import os
import json
import time
import hashlib
import threading
import uuid
import shutil
import copy
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Optional

from .constants import CONFIG_FILE, CACHE_DIR, diagnostic


# ---------------------------------------------------------------------------
# Safe persistence helpers
# ---------------------------------------------------------------------------

def _atomic_write_json(path: Path, data, keep_backup: bool = True):
    """Write JSON atomically in the target directory, retaining one backup."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    backup = path.with_name(path.name + ".bak")
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        if keep_backup and path.exists():
            shutil.copy2(path, backup)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _load_json_with_recovery(path: Path, default, label: str):
    """Load primary JSON, then its backup; never silently discard corruption."""
    if not path.exists():
        return copy.deepcopy(default)
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception as primary_exc:
        backup = path.with_name(path.name + ".bak")
        if backup.exists():
            try:
                data = json.loads(backup.read_text("utf-8"))
                diagnostic(
                    label + " was corrupt; recovered from " + backup.name +
                    " (" + str(primary_exc) + ")"
                )
                return data
            except Exception as backup_exc:
                diagnostic(
                    label + " and backup are unreadable: " + str(backup_exc)
                )
        else:
            diagnostic(label + " is unreadable: " + str(primary_exc))
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        corrupt = path.with_name(path.name + ".corrupt-" + stamp)
        try:
            os.replace(path, corrupt)
            diagnostic("Preserved unreadable file as " + corrupt.name)
        except Exception as move_exc:
            diagnostic("Could not preserve unreadable file: " + str(move_exc))
        return copy.deepcopy(default)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class JobConfig:
    job_id:             str       = field(default_factory=lambda: uuid.uuid4().hex)
    name:               str       = "New Job"
    source_path:        str       = ""
    ffs_exe:            str       = FFS_DEFAULT
    batch_file:         str       = ""
    log_file:           str       = ""
    threshold:          int       = 40
    hours_back:         int       = 24
    num_workers:        int       = 0
    exclude_patterns:   List[str] = field(default_factory=list)
    schedule_times:     List[str] = field(default_factory=list)
    enabled:            bool      = True
    guardian_folder:    str       = ""
    guardian_auto_pause: bool     = False

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        values = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        if not values.get("job_id"):
            values["job_id"] = uuid.uuid4().hex
        return cls(**values)


# ---------------------------------------------------------------------------
# Job store
# ---------------------------------------------------------------------------

class JobStore:
    def __init__(self, path: Path = CONFIG_FILE):
        self.path = path
        self.jobs: List[JobConfig] = []
        self._lock = threading.RLock()
        self._load()

    def _load(self):
        raw = _load_json_with_recovery(self.path, [], "Job configuration")
        try:
            jobs = [JobConfig.from_dict(d) for d in raw if isinstance(d, dict)]
            seen = set()
            migrated = False
            for job in jobs:
                if job.job_id in seen:
                    job.job_id = uuid.uuid4().hex
                    migrated = True
                seen.add(job.job_id)
            self.jobs = jobs
            # Persist newly assigned IDs from legacy configurations.
            if jobs and (migrated or any(
                "job_id" not in d for d in raw if isinstance(d, dict)
            )):
                self.save()
        except Exception as exc:
            diagnostic("Job configuration validation failed: " + str(exc))
            self.jobs = []

    def save(self):
        with self._lock:
            _atomic_write_json(self.path, [j.to_dict() for j in self.jobs])

    def add(self, job):
        with self._lock:
            self.jobs.append(job)
            self.save()

    def remove(self, i):
        with self._lock:
            self.jobs.pop(i)
            self.save()

    def update(self, i, j):
        with self._lock:
            self.jobs[i] = j
            self.save()


# ---------------------------------------------------------------------------
# Scan history (per-job, persisted as JSON, last 200 runs)
# ---------------------------------------------------------------------------

HISTORY_MAX = 200


class ScanHistory:
    def __init__(self, job_key: str):
        CACHE_DIR.mkdir(exist_ok=True)
        key      = hashlib.md5(job_key.encode()).hexdigest()[:12]
        self._fp = CACHE_DIR / ("history_" + key + ".json")
        self.records: List[dict] = []
        self._load()

    def _load(self):
        raw = _load_json_with_recovery(self._fp, [], "Scan history")
        self.records = raw if isinstance(raw, list) else []

    def add(self, record: dict):
        self.records.insert(0, record)          # newest first
        if len(self.records) > HISTORY_MAX:
            self.records = self.records[:HISTORY_MAX]
        try:
            _atomic_write_json(self._fp, self.records)
        except Exception as exc:
            diagnostic("Could not save scan history: " + str(exc))

    def clear(self):
        self.records = []
        try:
            self._fp.unlink(missing_ok=True)
            self._fp.with_name(self._fp.name + ".bak").unlink(missing_ok=True)
        except Exception as exc:
            diagnostic("Could not clear scan history: " + str(exc))


# ---------------------------------------------------------------------------
# Scan cache (per-source-path, stores file/dir mtimes between runs)
# ---------------------------------------------------------------------------

class ScanCache:
    def __init__(self, source_path: str):
        CACHE_DIR.mkdir(exist_ok=True)
        key      = hashlib.md5(source_path.encode()).hexdigest()[:12]
        self._fp = CACHE_DIR / ("cache_" + key + ".json")
        self._data: dict = {}
        self._dirs: dict = {}
        self._ts:   float = 0.0
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        raw = _load_json_with_recovery(self._fp, {}, "Scan cache")
        try:
            self._data = {
                os.path.normpath(k): v
                for k, v in raw.get("files", {}).items()
            }
            self._dirs = {
                os.path.normpath(k): v
                for k, v in raw.get("dirs", {}).items()
            }
            self._ts = float(raw.get("ts", 0.0))
        except Exception as exc:
            diagnostic("Scan cache validation failed: " + str(exc))
            self._data, self._dirs, self._ts = {}, {}, 0.0

    def save(self, files: dict, dirs: dict):
        # Normalise all keys before saving so forward/backward slashes
        # never cause cache misses on the next load.
        norm_files = {os.path.normpath(k): v for k, v in files.items()}
        norm_dirs  = {os.path.normpath(k): v for k, v in dirs.items()}
        with self._lock:
            self._data = norm_files
            self._dirs = norm_dirs
            self._ts   = time.time()
        try:
            _atomic_write_json(
                self._fp,
                {"files": norm_files, "dirs": norm_dirs, "ts": self._ts}
            )
        except Exception as exc:
            diagnostic("Could not save scan cache: " + str(exc))
            raise

    def clear(self):
        with self._lock:
            self._data = {}
            self._dirs = {}
            self._ts   = 0.0
        try:
            self._fp.unlink(missing_ok=True)
            self._fp.with_name(self._fp.name + ".bak").unlink(missing_ok=True)
        except Exception as exc:
            diagnostic("Could not clear scan cache: " + str(exc))

    @property
    def known_total(self) -> int:
        with self._lock:
            return len(self._data)

    @property
    def age_str(self) -> str:
        if self._ts == 0:
            return "never"
        secs = time.time() - self._ts
        if secs < 120:
            return str(int(secs)) + "s ago"
        if secs < 3600:
            return str(int(secs / 60)) + "m ago"
        return str(int(secs / 3600)) + "h ago"

    def dir_unchanged(self, dir_path: str, mtime: float) -> bool:
        with self._lock:
            c = self._dirs.get(os.path.normpath(dir_path))
        return c is not None and abs(c - mtime) < 1.0

    def file_mtime(self, fp: str) -> Optional[float]:
        with self._lock:
            v = self._data.get(os.path.normpath(fp))
        return float(v) if v is not None else None
