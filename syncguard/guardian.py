"""
SyncGuard folder guardian — real-time rename-deny and delete protection
using watchdog file system events.
"""

import os
import time
import queue as _queue
import threading
from datetime import datetime
from dataclasses import dataclass
from typing import Optional

from watchdog.observers import Observer as _WdObserver
from watchdog.events import (
    FileSystemEventHandler as _WdHandler,
)


# ---------------------------------------------------------------------------
# Cooldown (prevents revert-loops)
# ---------------------------------------------------------------------------

class _GuardianCooldown:
    """Prevents revert-loops by ignoring events on recently-touched paths."""

    def __init__(self, cooldown_ms: int = 500):
        self._secs = cooldown_ms / 1000.0
        self._lock = threading.Lock()
        self._ts: dict = {}

    def mark(self, path: str):
        with self._lock:
            now = time.time()
            self._ts[path] = now
            self._ts = {
                p: t for p, t in self._ts.items()
                if now - t < self._secs
            }

    def mark_rename(self, src: str, dst: str):
        self.mark(src)
        self.mark(dst)

    def is_on_cooldown(self, path: str) -> bool:
        with self._lock:
            t = self._ts.get(path)
            if t is None:
                return False
            if time.time() - t < self._secs:
                return True
            del self._ts[path]
            return False


# ---------------------------------------------------------------------------
# Watchdog event handler
# ---------------------------------------------------------------------------

class _GuardianEventHandler(_WdHandler):
    """Queues renames and deletes for the worker threads."""

    def __init__(self, rename_q: _queue.Queue, delete_q: _queue.Queue,
                 cooldown: _GuardianCooldown, paused_getter):
        super().__init__()
        self._rq      = rename_q
        self._dq      = delete_q
        self._cool    = cooldown
        self._paused  = paused_getter

    def on_moved(self, event):
        if self._paused():
            return
        if (self._cool.is_on_cooldown(event.src_path) or
                self._cool.is_on_cooldown(event.dest_path)):
            return
        self._rq.put((
            'rename', event.src_path, event.dest_path, event.is_directory))

    def on_deleted(self, event):
        if self._paused():
            return
        if self._cool.is_on_cooldown(event.src_path):
            return
        self._dq.put(('delete', event.src_path, event.is_directory))


# ---------------------------------------------------------------------------
# Rename reverter (worker thread)
# ---------------------------------------------------------------------------

class _RenameReverter:
    """Moves renamed files/folders back to their original path."""

    def __init__(self, log_cb, cooldown: _GuardianCooldown):
        self.queue   = _queue.Queue()
        self._log    = log_cb
        self._cool   = cooldown
        self._run    = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="sg-guardian-rename")
        self._thread.start()

    def _loop(self):
        import shutil as _shutil
        while self._run:
            try:
                item = self.queue.get(timeout=0.5)
            except _queue.Empty:
                continue
            try:
                _, src, dst, _ = item
                if not os.path.exists(src) and os.path.exists(dst):
                    self._log(
                        "Guardian: reverting rename  " +
                        os.path.basename(dst) + " → " +
                        os.path.basename(src), "WARN")
                    os.makedirs(os.path.dirname(src), exist_ok=True)
                    _shutil.move(dst, src)
                    self._cool.mark_rename(src, dst)
                    self._log(
                        "Guardian: rename denied  ✓  " +
                        os.path.basename(src), "OK")
                elif os.path.exists(src):
                    pass   # already back in place
                else:
                    self._log(
                        "Guardian: both paths gone, cannot revert  " +
                        src + " → " + dst, "WARN")
            except Exception as exc:
                self._log(
                    "Guardian: rename revert error  " + str(exc), "ERROR")

    def stop(self):
        self._run = False
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Deletion guard (worker thread)
# ---------------------------------------------------------------------------

class _DeletionGuard:
    """
    Worker thread for delete events.
    Only restores the special .guardian_lockfile; all other deletions are
    logged but not restored (content must come from FreeFileSync restore).
    """

    def __init__(self, log_cb, cooldown: _GuardianCooldown):
        self.queue   = _queue.Queue()
        self._log    = log_cb
        self._cool   = cooldown
        self._run    = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="sg-guardian-del")
        self._thread.start()

    def _loop(self):
        while self._run:
            try:
                item = self.queue.get(timeout=0.5)
            except _queue.Empty:
                continue
            try:
                _, path, _ = item
                if os.path.exists(path):
                    continue
                if path.endswith(".guardian_lockfile"):
                    self._log(
                        "Guardian: lockfile deleted – restoring  " +
                        os.path.basename(path), "WARN")
                    os.makedirs(
                        os.path.dirname(path) or ".", exist_ok=True)
                    with open(path, "w") as f:
                        f.write(
                            "Guardian lockfile – protected since " +
                            datetime.now().isoformat() + "\n")
                    self._cool.mark(path)
                    self._log("Guardian: lockfile restored  ✓", "OK")
                else:
                    self._log(
                        "Guardian: deletion detected (not restored)  " +
                        path, "WARN")
            except Exception as exc:
                self._log(
                    "Guardian: deletion handler error  " + str(exc), "ERROR")

    def stop(self):
        self._run = False
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Guardian state dataclass
# ---------------------------------------------------------------------------

@dataclass
class _GuardianState:
    """All runtime state for one job's guardian, keyed by job name."""
    folder:          str             = ""
    observer:        Optional[object] = None
    rename_reverter: Optional[object] = None
    deletion_guard:  Optional[object] = None
    cooldown:        Optional[object] = None
    is_monitoring:   bool            = False
    paused:          bool            = False
    auto_paused:     bool            = False
    auto_job_id:     Optional[str]   = None
