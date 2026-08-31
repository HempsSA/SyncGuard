"""
SyncGuard entry point — bootstraps dependencies, then launches the GUI.

Usage:
    python -m syncguard
"""

import os
import sys
import subprocess
import tempfile


# ---------------------------------------------------------------------------
# Single-instance lock (cross-platform)
# ---------------------------------------------------------------------------
_LOCK_FILE = os.path.join(tempfile.gettempdir(), "syncguard_instance.lock")
_lock_handle = None


def _acquire_instance_lock():
    """Try to acquire a file lock. Returns True if this is the only instance."""
    global _lock_handle
    try:
        if os.name == "nt":
            import msvcrt
            _lock_handle = open(_LOCK_FILE, "w")
            try:
                msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
                _lock_handle.write(str(os.getpid()))
                _lock_handle.flush()
                return True
            except OSError:
                _lock_handle.close()
                _lock_handle = None
                return False
        else:
            # POSIX: use fcntl
            import fcntl
            _lock_handle = open(_LOCK_FILE, "w")
            try:
                fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                _lock_handle.write(str(os.getpid()))
                _lock_handle.flush()
                return True
            except OSError:
                _lock_handle.close()
                _lock_handle = None
                return False
    except Exception:
        return True   # if locking fails, let the app start anyway


def _release_instance_lock():
    global _lock_handle
    if _lock_handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_UN)
        _lock_handle.close()
    except Exception:
        pass
    _lock_handle = None
    try:
        os.unlink(_LOCK_FILE)
    except Exception:
        pass


def _require(package, import_name=None):
    import importlib
    name = import_name or package
    try:
        return importlib.import_module(name)
    except ImportError:
        print("Installing " + package + "...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", package, "-q"],
            stdout=subprocess.DEVNULL)
        return importlib.import_module(name)


# Install all dependencies before importing the app modules.
_require("customtkinter")
_require("schedule")
_require("pystray")
_require("Pillow", "PIL")
_require("watchdog")
_require("psutil")


def main():
    if not _acquire_instance_lock():
        from tkinter import messagebox
        import tkinter as tk
        _root = tk.Tk()
        _root.withdraw()
        messagebox.showwarning(
            "SyncGuard",
            "SyncGuard is already running.\n"
            "Check the system tray.")
        _root.destroy()
        sys.exit(0)

    from .app import SyncGuardApp
    app = SyncGuardApp()
    try:
        app.mainloop()
    finally:
        _release_instance_lock()


if __name__ == "__main__":
    main()
