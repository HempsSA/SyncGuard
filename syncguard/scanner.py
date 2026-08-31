"""
SyncGuard scanner — parallel directory scanning with Windows-native
FindFirstFileExW and change-rate detection (ChangeGuard).
"""

import os
import sys
import time
import stat
import fnmatch
import threading
import subprocess
import queue as _queue
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List, Optional

from .persistence import ScanCache, JobConfig
from .ransomware import (
    sample_entropy, detect_suspicious_extensions, compute_anomaly_score,
)
from .snapshot import (
    capture_manifest, save_snapshot, compare_snapshots, load_snapshot,
)


# ---------------------------------------------------------------------------
# Scan progress dataclass
# ---------------------------------------------------------------------------

@dataclass
class ScanProgress:
    scanned:       int   = 0
    changed:       int   = 0
    total_hint:    int   = 0
    skipped_dirs:  int   = 0
    active_dirs:   int   = 0
    current_dir:   str   = ""
    files_per_sec: float = 0.0
    warm:          bool  = False
    engine:        str   = "parallel"
    workers:       int   = 0


# ---------------------------------------------------------------------------
# Windows native directory listing (no per-file stat calls)
# ---------------------------------------------------------------------------

_IS_WIN = sys.platform == "win32"

if _IS_WIN:
    import ctypes
    import ctypes.wintypes as _wt

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLow", _wt.DWORD), ("dwHigh", _wt.DWORD)]

    class _WIN32_FIND_DATAW(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes",   _wt.DWORD),
            ("ftCreationTime",     _FILETIME),
            ("ftLastAccessTime",   _FILETIME),
            ("ftLastWriteTime",    _FILETIME),
            ("nFileSizeHigh",      _wt.DWORD),
            ("nFileSizeLow",       _wt.DWORD),
            ("dwReserved0",        _wt.DWORD),
            ("dwReserved1",        _wt.DWORD),
            ("cFileName",          _wt.WCHAR * 260),
            ("cAlternateFileName", _wt.WCHAR * 14),
        ]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # Set argtypes and restype on every call - without these ctypes guesses
    # argument sizes incorrectly on 64-bit Windows and causes access violations.
    _k32.FindFirstFileExW.argtypes = [
        _wt.LPCWSTR,       # lpFileName (pattern)
        ctypes.c_int,      # fInfoLevelId  (FINDEX_INFO_LEVELS enum)
        ctypes.c_void_p,   # lpFindFileData (cast from byref later)
        ctypes.c_int,      # fSearchOp     (FINDEX_SEARCH_OPS enum)
        ctypes.c_void_p,   # lpSearchFilter (must be NULL)
        _wt.DWORD,         # dwAdditionalFlags
    ]
    _k32.FindFirstFileExW.restype  = _wt.HANDLE
    _k32.FindNextFileW.argtypes    = [_wt.HANDLE, ctypes.c_void_p]
    _k32.FindNextFileW.restype     = _wt.BOOL
    _k32.FindClose.argtypes        = [_wt.HANDLE]
    _k32.FindClose.restype         = _wt.BOOL

    _INVALID_HANDLE  = _wt.HANDLE(-1).value
    _FILE_ATTR_DIR     = 0x10
    _FILE_ATTR_REPARSE = 0x400    # symlink / junction - skip like FreeFileSync
    _FILE_ATTR_VIRTUAL = 0x10000  # virtual DFS entry  - skip
    _FindExInfoBasic   = 1
    _FindExSearchNameMatch = 0
    _FIND_FIRST_EX_LARGE_FETCH = 0x2   # Windows 8+ / SMB3
    _WIN_TICK   = 10_000_000
    _EPOCH_DIFF = 11_644_473_600

    def _ft2unix(ft: _FILETIME) -> float:
        return ((ft.dwHigh << 32) | ft.dwLow) / _WIN_TICK - _EPOCH_DIFF

    def _long_path(path: str) -> str:
        """Add long-path prefix for paths > 200 chars."""
        if len(path) <= 200 or path.startswith("\\\\?\\"):
            return path
        if path.startswith("\\\\"):
            return "\\\\?\\UNC\\" + path[2:]
        return "\\\\?\\" + path

    def _winapi_listdir(path: str, large_fetch: bool):
        """
        Single attempt at FindFirstFileExW. Returns (files, dirs) or raises.
        files: [(path, mtime, size), ...]
        dirs:  [(path, mtime), ...]
        """
        lp      = _long_path(path)
        pattern = lp.rstrip("\\") + "\\*"
        base    = path.rstrip("\\") + "\\"
        data    = _WIN32_FIND_DATAW()
        flags   = _FIND_FIRST_EX_LARGE_FETCH if large_fetch else 0

        handle = _k32.FindFirstFileExW(
            pattern,
            _FindExInfoBasic,
            ctypes.cast(ctypes.byref(data), ctypes.c_void_p),
            _FindExSearchNameMatch,
            None,
            flags)

        if handle == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())

        files, dirs = [], []
        try:
            while True:
                name  = data.cFileName
                attrs = data.dwFileAttributes
                if name not in (".", ".."):
                    if not (attrs & (_FILE_ATTR_REPARSE | _FILE_ATTR_VIRTUAL)):
                        full  = base + name
                        mtime = _ft2unix(data.ftLastWriteTime)
                        if attrs & _FILE_ATTR_DIR:
                            dirs.append((full, mtime))
                        else:
                            fsize = (data.nFileSizeHigh << 32) | data.nFileSizeLow
                            files.append((full, mtime, fsize))
                if not _k32.FindNextFileW(
                        handle,
                        ctypes.cast(ctypes.byref(data), ctypes.c_void_p)):
                    break
        finally:
            _k32.FindClose(handle)
        return files, dirs

    # Probe which listing method works best for a given root path.
    _listdir_method: dict = {}

    def _fast_listdir(path: str, _log=None):
        """
        Enumerate a directory. Probes the best available method once per root,
        then reuses it for all subdirs.  Falls back gracefully to os.scandir.
        """
        p = path.replace("/", "\\")
        parts = p.split("\\")
        root_key = (
            parts[0] + "\\" if (len(parts) > 0 and parts[0].endswith(":"))
            else "\\\\".join(parts[:4]) if p.startswith("\\\\")
            else p[:3]
        )

        method = _listdir_method.get(root_key)

        if method is None:
            for try_large, label in ((True, "large"), (False, "small")):
                try:
                    result = _winapi_listdir(path, try_large)
                    _listdir_method[root_key] = label
                    if _log:
                        _log("Scanner method for " + root_key + ": WinAPI/" + label)
                    return result
                except OSError:
                    pass
            _listdir_method[root_key] = "scandir"
            if _log:
                _log("Scanner method for " + root_key + ": os.scandir (WinAPI unavailable)")
            method = "scandir"

        if method in ("large", "small"):
            try:
                return _winapi_listdir(path, method == "large")
            except OSError:
                pass

        # os.scandir path
        files, dirs = [], []
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        st = e.stat(follow_symlinks=False)
                        if stat.S_ISDIR(st.st_mode):
                            dirs.append((e.path, st.st_mtime))
                        elif stat.S_ISREG(st.st_mode):
                            files.append((e.path, st.st_mtime, st.st_size))
                    except OSError:
                        raise
        except (PermissionError, OSError):
            raise
        return files, dirs

else:
    def _fast_listdir(path: str):
        """Non-Windows fallback. Returns same format as the Windows version."""
        files, dirs = [], []
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        if e.is_symlink():          # skip symlinks
                            continue
                        st = e.stat(follow_symlinks=False)
                        if stat.S_ISDIR(st.st_mode):
                            dirs.append((e.path, st.st_mtime))
                        elif stat.S_ISREG(st.st_mode):
                            files.append((e.path, st.st_mtime, st.st_size))
                    except OSError:
                        raise
        except (PermissionError, OSError):
            raise
        return files, dirs


# ---------------------------------------------------------------------------
# Network path detection
# ---------------------------------------------------------------------------

def _is_network_path(path: str) -> bool:
    p = path.strip()
    if p.startswith("\\\\") or p.startswith("//"):
        return True
    if _IS_WIN and len(p) >= 2 and p[1] == ":":
        try:
            drive = p[:3]
            return ctypes.windll.kernel32.GetDriveTypeW(drive) == 4
        except Exception:
            pass
    return False


# ---------------------------------------------------------------------------
# Parallel scanner
# ---------------------------------------------------------------------------

class ParallelScanner:
    # DDR3 optimisation: local threads capped at half logical cores (max 4).
    _AUTO_LOCAL   = max(2, min(4, (os.cpu_count() or 4) // 2))
    # Network scanning is latency-bound; 8 workers hide most of the RTT.
    _AUTO_NETWORK = 8

    def __init__(self, root: str, cache: ScanCache,
                 num_workers: int = 0, exclude_patterns: List[str] = None,
                 progress_cb=None, log_cb=None):
        self._fnmatch = fnmatch
        self._root    = os.path.normpath(root)
        self._cache   = cache
        self._prog_cb = progress_cb or (lambda p: None)
        self._log_cb  = log_cb or (lambda m, l="INFO": None)
        self._excludes = [p.lower() for p in (exclude_patterns or [])]

        self._workers = (
            num_workers if num_workers > 0 else
            (self._AUTO_NETWORK if _is_network_path(root)
             else self._AUTO_LOCAL)
        )

        # Pre-index cache by directory for O(1) lookups.
        self._dir_idx: dict = {}
        for fp, val in cache._data.items():
            fp_norm = os.path.normpath(fp)
            d       = os.path.dirname(fp_norm)
            self._dir_idx.setdefault(d, {})[fp_norm] = val

        self._lock      = threading.Lock()
        self._total     = 0
        self._changed   = 0
        self._skipped   = 0
        self._new_files: dict = {}
        self._new_dirs:  dict = {}
        self._active    = 0
        self._done      = threading.Event()
        self._dir_queue: _queue.Queue = _queue.Queue()
        self._last_emit = 0.0
        self._start_t   = 0.0
        self._last_dir  = ""
        self._aborted   = False
        self._errors: List[str] = []

        # Control events
        self._stop_evt  = threading.Event()
        self._pause_evt = threading.Event()   # set = paused, clear = running
        self._paused    = False

    # -- Control API --------------------------------------------------------

    def stop(self):
        """Signal all worker threads to abort immediately."""
        self._aborted = True
        self._stop_evt.set()
        self._pause_evt.clear()
        self._done.set()

    def pause(self):
        """Pause all worker threads between directories."""
        self._paused = True
        self._pause_evt.set()

    def resume(self):
        """Resume paused worker threads."""
        self._paused = False
        self._pause_evt.clear()

    @property
    def is_paused(self) -> bool:
        return self._paused

    # -- Internals ----------------------------------------------------------

    def _emit(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_emit < 0.40:
            return
        self._last_emit = now
        elapsed = now - self._start_t
        fps = self._total / elapsed if elapsed > 0 else 0.0
        with self._lock:
            p = ScanProgress(
                scanned=self._total, changed=self._changed,
                total_hint=self._cache.known_total,
                skipped_dirs=self._skipped, active_dirs=self._active,
                current_dir=self._last_dir, files_per_sec=fps,
                warm=self._cache.known_total > 0,
                engine="parallel", workers=self._workers,
            )
        self._prog_cb(p)

    def _process_dir(self, dir_path: str, parent_mtime: Optional[float]):
        if self._stop_evt.is_set():
            with self._lock:
                self._active -= 1
                if self._active == 0:
                    self._done.set()
            return

        while self._pause_evt.is_set():
            if self._stop_evt.is_set():
                with self._lock:
                    self._active -= 1
                    if self._active == 0:
                        self._done.set()
                return
            time.sleep(0.05)

        local_files: dict = {}
        local_dirs:  dict = {}
        local_chg    = 0
        local_tot    = 0
        local_skip   = False
        subdirs      = []

        try:
            if parent_mtime is not None:
                dir_mtime = parent_mtime
            else:
                try:
                    dir_mtime = os.stat(dir_path).st_mtime
                except OSError:
                    dir_mtime = 0.0

            dir_path_n     = os.path.normpath(dir_path)
            dir_name_lower = os.path.basename(dir_path_n).lower()

            if self._excludes and any(
                self._fnmatch.fnmatch(dir_name_lower, pat)
                for pat in self._excludes
            ):
                return

            local_dirs[dir_path_n] = dir_mtime

            if self._cache.dir_unchanged(dir_path_n, dir_mtime):
                cached = self._dir_idx.get(dir_path_n, {})
                for fp, val in cached.items():
                    local_tot += 1
                    local_files[fp] = val
                local_skip = True
                try:
                    _raw_files, raw_dirs = _fast_listdir(dir_path)
                    subdirs = [(p, m) for p, m in raw_dirs]
                except OSError as exc:
                    message = "Cannot list dir: " + dir_path + " - " + str(exc)
                    with self._lock:
                        self._errors.append(message)
                    self._log_cb(message, "ERROR")
            else:
                try:
                    raw_files, raw_dirs = _fast_listdir(dir_path)
                except OSError as exc:
                    message = "Cannot list dir: " + dir_path + " - " + str(exc)
                    with self._lock:
                        self._errors.append(message)
                    self._log_cb(message, "ERROR")
                    return
                subdirs = raw_dirs
                cached  = self._dir_idx.get(dir_path_n, {})

                for entry in raw_files:
                    fpath, mtime, fsize = entry
                    if self._excludes:
                        fname_lower = os.path.basename(fpath).lower()
                        if any(self._fnmatch.fnmatch(fname_lower, pat)
                               for pat in self._excludes):
                            continue
                    fpath_n = os.path.normpath(fpath)
                    local_tot += 1
                    local_files[fpath_n] = [mtime, fsize]
                    cached_val = cached.get(fpath_n)
                    if cached_val is None:
                        local_chg += 1
                    else:
                        if isinstance(cached_val, list):
                            c_mtime, c_size = cached_val[0], cached_val[1]
                        else:
                            c_mtime, c_size = float(cached_val), -1
                        if abs(mtime - c_mtime) > 1.0 or (
                            c_size >= 0 and fsize != c_size
                        ):
                            local_chg += 1

        except Exception as exc:
            message = "Unexpected error scanning " + dir_path + ": " + str(exc)
            with self._lock:
                self._errors.append(message)
            self._log_cb(message, "ERROR")
        finally:
            with self._lock:
                self._total   += local_tot
                self._changed += local_chg
                if local_skip:
                    self._skipped += 1
                self._new_files.update(local_files)
                self._new_dirs.update(local_dirs)
                self._last_dir = dir_path
                for sd_path, sd_mtime in subdirs:
                    self._active += 1
                    self._dir_queue.put((sd_path, sd_mtime))
                self._active -= 1
                if self._active == 0:
                    self._done.set()
            self._emit()

    def run(self):
        self._start_t = time.monotonic()
        self._log_cb(
            "Parallel scan: " + self._root +
            "  workers=" + str(self._workers) +
            (" [WARM]" if self._cache.known_total > 0 else " [COLD]"))

        with self._lock:
            self._active = 1
        self._dir_queue.put((self._root, None))

        with ThreadPoolExecutor(
            max_workers=self._workers, thread_name_prefix="sg-scan"
        ) as ex:
            while True:
                drained = 0
                while True:
                    try:
                        d, d_mtime = self._dir_queue.get_nowait()
                        ex.submit(self._process_dir, d, d_mtime)
                        drained += 1
                    except _queue.Empty:
                        break

                if self._done.is_set():
                    while True:
                        try:
                            d, d_mtime = self._dir_queue.get_nowait()
                            ex.submit(self._process_dir, d, d_mtime)
                        except _queue.Empty:
                            break
                    break

                if drained == 0:
                    self._done.wait(timeout=0.05)

        self._emit(force=True)

        if self._aborted:
            self._log_cb("Scan aborted by user.", "WARN")

        return (self._new_files, self._new_dirs,
                self._total, self._changed, self._skipped, self._aborted,
                list(self._errors))


# ---------------------------------------------------------------------------
# Change guard (scan → decide → launch FreeFileSync)
# ---------------------------------------------------------------------------

class ChangeGuard:
    def __init__(self, job: JobConfig, log_cb=None, progress_cb=None,
                 override_cb=None, scanner_ready_cb=None):
        self.job              = job
        self.log_cb           = log_cb           or (lambda msg, level="INFO": None)
        self.progress_cb      = progress_cb      or (lambda p: None)
        self.override_cb      = override_cb
        self.scanner_ready_cb = scanner_ready_cb

    def _log(self, msg, level="INFO"):
        self.log_cb(msg, level)

    def scan(self):
        root = self.job.source_path
        if not root:
            self._log("No source path configured.", "ERROR")
            raise ValueError("No source path configured")
        if not os.path.exists(root):
            self._log(
                "Source path does not exist or is not accessible: " + root,
                "ERROR")
            raise OSError("Source path not accessible: " + root)

        cache = ScanCache(root)
        root = os.path.normpath(root)
        try:
            _diag_files, _diag_dirs = _fast_listdir(root)
            self._log(
                "Path OK: " + str(len(_diag_files)) + " file(s), " +
                str(len(_diag_dirs)) + " subdir(s) in root  [" + root + "]")
        except Exception as exc:
            self._log(
                "Cannot list root path '" + root + "': " + str(exc), "ERROR")
            raise

        scanner = ParallelScanner(
            root, cache, num_workers=self.job.num_workers,
            exclude_patterns=self.job.exclude_patterns,
            progress_cb=self.progress_cb, log_cb=self._log_cb)
        self._scanner = scanner
        if self.scanner_ready_cb is not None:
            self.scanner_ready_cb(scanner)

        was_cold = cache.known_total == 0
        new_files, new_dirs, total, changed, skipped, aborted, errors = (
            scanner.run()
        )
        if aborted:
            return None
        if errors:
            self._log(
                "SCAN INCOMPLETE: " + str(len(errors)) +
                " directorie(s) could not be read. Cache retained; sync blocked.",
                "ERROR")
            return {"incomplete": True, "errors": errors}

        deleted = len([
            f for f in cache._data
            if os.path.normpath(f) not in new_files
        ])
        changed += deleted
        total += deleted
        pct = round(changed / total * 100, 2) if total > 0 else 0.0
        self._log(
            "Total: " + str(total) + "  Changed: " + str(changed) +
            "  Deleted: " + str(deleted) + "  Skipped dirs: " +
            str(skipped) + "  Rate: " + str(pct) + "%" +
            ("  [BASELINE - first scan]" if was_cold else ""))
        return {
            "incomplete": False, "total": total, "changed": changed,
            "pct": pct, "was_cold": was_cold, "cache": cache,
            "new_files": new_files, "new_dirs": new_dirs,
        }

    def check_and_run(self) -> str:
        self._scanner = None
        self._last_total = 0
        self._last_changed = 0
        self._last_pct = 0.0
        self._last_anomaly = None
        self._pre_snapshot = None
        self._log("=== Job: " + self.job.name + " ===")
        try:
            result = self.scan()
        except Exception as exc:
            self._log("Scan failed: " + str(exc), "ERROR")
            return "ERROR"

        if result is None:
            return "ABORTED"
        if result.get("incomplete"):
            return "ERROR"

        total = result["total"]
        changed = result["changed"]
        pct = result["pct"]
        was_cold = result["was_cold"]
        self._last_total = total
        self._last_changed = changed
        self._last_pct = pct

        if total == 0:
            self._log(
                "No files found in source path. Cache retained; sync blocked.",
                "WARN")
            return "WARN"

        if was_cold:
            try:
                result["cache"].save(result["new_files"], result["new_dirs"])
            except Exception as exc:
                self._log("Baseline could not be saved: " + str(exc), "ERROR")
                return "ERROR"
            self._log(
                "BASELINE CREATED: " + str(total) +
                " files indexed. FreeFileSync was NOT launched. "
                "Run the job again after reviewing the source.",
                "WARN")
            return "WARN"

        # --- Ransomware pre-sync checks ---
        if self.job.ransomware_protection and changed > 0:
            anomaly = self._run_ransomware_checks(
                result["new_files"], total, changed, deleted=0)
            self._last_anomaly = anomaly
            if anomaly.is_blocked:
                self._log(
                    "RANSOMWARE ALERT: anomaly score " +
                    str(anomaly.score) + "/100  BLOCKED",
                    "ERROR")
                for reason in anomaly.reasons:
                    self._log("  > " + reason, "ERROR")
                return "ABORTED"

        # --- Threshold gate (original logic) ---
        if pct > self.job.threshold:
            self._log(
                "THRESHOLD EXCEEDED: " + str(pct) + "% changed - limit is " +
                str(self.job.threshold) + "%.", "WARN")
            proceed = (
                self.override_cb(pct, total, changed)
                if self.override_cb else False
            )
            if not proceed:
                self._log(
                    "ABORTED: threshold exceeded. Trusted cache retained.",
                    "ERROR")
                return "ABORTED"
            self._log(
                "User approved threshold override. Launching FreeFileSync...",
                "WARN")
        else:
            self._log(
                "Change rate " + str(pct) +
                "% is within threshold. Launching FreeFileSync...")

        if not self.job.ffs_exe or not self.job.batch_file:
            self._log(
                "FFS exe or batch file not configured. Trusted cache retained.",
                "ERROR")
            return "ERROR"

        # --- Pre-sync destination snapshot ---
        dest = self.job.destination_path
        if (self.job.ransomware_protection and
                self.job.snapshot_before_sync and dest):
            self._pre_snapshot = self._capture_dest_snapshot(dest)

        # --- Launch FreeFileSync ---
        try:
            proc = subprocess.run(
                [self.job.ffs_exe, self.job.batch_file],
                capture_output=True, text=True)
            msgs = {
                0: ("FreeFileSync finished successfully.", "OK"),
                1: ("FreeFileSync finished with warnings (code 1).", "WARN"),
                3: ("FreeFileSync was aborted by user (code 3).", "WARN"),
            }
            msg, lvl = msgs.get(
                proc.returncode,
                ("FreeFileSync exited with code " +
                 str(proc.returncode) + ".", "ERROR"))
            self._log(msg, lvl)

            # --- Post-sync validation ---
            if (self.job.ransomware_protection and
                    dest and proc.returncode in (0, 1)):
                self._post_sync_validate(dest)

            if proc.returncode in (0, 1):
                try:
                    result["cache"].save(
                        result["new_files"], result["new_dirs"])
                    self._log(
                        "Trusted scan cache committed after approved "
                        "FreeFileSync completion.")
                except Exception as exc:
                    self._log(
                        "FreeFileSync completed, but cache commit failed: " +
                        str(exc), "ERROR")
                    return "ERROR"
            else:
                self._log(
                    "Trusted cache retained because FreeFileSync did not "
                    "complete successfully.", "WARN")
            return lvl
        except Exception as exc:
            self._log(
                "Failed to launch FreeFileSync: " + str(exc) +
                ". Trusted cache retained.", "ERROR")
            return "ERROR"

    # ------------------------------------------------------------------- # Ransomware protection helpers
    # -------------------------------------------------------------------

    def _run_ransomware_checks(self, new_files, total, changed,
                               deleted=0):
        """Run entropy, extension, and anomaly checks. Return score."""
        # 1. Entropy sampling on changed files
        changed_paths = list(new_files.keys())[:changed]
        ent_result = sample_entropy(
            changed_paths,
            threshold=self.job.entropy_threshold)
        if ent_result.sampled > 0:
            self._log(
                "Entropy check: sampled " + str(ent_result.sampled) +
                " files, avg=" + str(ent_result.avg_entropy) +
                ", max=" + str(ent_result.max_entropy) +
                ", high=" + str(ent_result.high_entropy))
            if ent_result.is_suspicious:
                self._log(
                    "Entropy ALERT: " + str(ent_result.high_entropy) +
                    "/" + str(ent_result.sampled) +
                    " files have suspicious entropy", "WARN")

        # 2. Extension anomaly detection
        ext_result = detect_suspicious_extensions(
            changed_paths,
            extra_blocklist=set(self.job.custom_extensions))
        if ext_result.suspicious > 0:
            self._log(
                "Extension check: " + str(ext_result.suspicious) +
                "/" + str(ext_result.total_changed) +
                " suspicious extensions", "WARN")
            for ext, count in ext_result.suspicious_exts.items():
                self._log(
                    "  > " + ext + ": " + str(count) + " files",
                    "WARN")

        # 3. Composite anomaly score
        score = compute_anomaly_score(
            change_pct=self.job.threshold * changed / total * 100 / max(self.job.threshold, 1),
            total_files=total,
            changed_files=changed,
            deleted_files=deleted,
            entropy_result=ent_result,
            extension_result=ext_result,
            block_threshold=self.job.anomaly_block_score)
        self._log(
            "Anomaly score: " + str(score.score) +
            "/100 (block threshold: " +
            str(self.job.anomaly_block_score) + ")")
        return score

    def _capture_dest_snapshot(self, dest_path):
        """Capture destination manifest before sync."""
        self._log("Snapshot: capturing destination manifest...")
        manifest = capture_manifest(
            dest_path, self.job.job_id, log_cb=self._log)
        if manifest:
            fp = save_snapshot(manifest)
            self._log(
                "Snapshot saved: " + str(fp.name) +
                " (" + str(manifest.total_files) + " files)")
        return manifest

    def _post_sync_validate(self, dest_path):
        """Validate destination integrity after sync."""
        if self._pre_snapshot is None:
            return
        self._log("Post-sync validation: checking destination...")
        post = capture_manifest(
            dest_path, self.job.job_id, log_cb=self._log)
        if post is None:
            self._log(
                "Post-sync validation: could not read destination",
                "WARN")
            return
        diff = compare_snapshots(self._pre_snapshot, post)
        if diff.is_clean:
            self._log("Post-sync validation: OK - " + diff.summary)
        else:
            self._log(
                "Post-sync ANOMALY: " + diff.summary, "ERROR")
            if diff.hash_mismatch:
                self._log(
                    "  Hash mismatches (possible corruption):", "ERROR")
                for f in diff.hash_mismatch[:5]:
                    self._log("    - " + f, "ERROR")
            if diff.size_anomaly:
                self._log(
                    "  Size anomalies (possible truncation):", "ERROR")
                for f in diff.size_anomaly[:5]:
                    self._log("    - " + f, "ERROR")
