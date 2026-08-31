"""
SyncGuard entry point — bootstraps dependencies, then launches the GUI.

Usage:
    python -m syncguard
"""

import sys
import subprocess


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
    from .app import SyncGuardApp
    app = SyncGuardApp()
    app._build_tray_icon()
    app.bind("<Unmap>", app._on_minimize)
    app.mainloop()


if __name__ == "__main__":
    main()
