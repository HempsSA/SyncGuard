"""
SyncGuard constants — paths, colour palette, and diagnostic helpers.
"""

import sys
from pathlib import Path
from typing import List

# ---------------------------------------------------------------------------
# App identity
# ---------------------------------------------------------------------------
APP_NAME = "SyncGuard"

# ---------------------------------------------------------------------------
# Paths (relative to the *project root*, i.e. one level above the package)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE  = PROJECT_ROOT / "syncguard_jobs.json"
CACHE_DIR    = PROJECT_ROOT / "syncguard_cache"

LOG_MAX   = 500   # lines kept in the activity log widget (~40 KB)
FFS_DEFAULT = r"C:\Program Files\FreeFileSync\FreeFileSync.exe"

# ---------------------------------------------------------------------------
# Colour palette (GitHub-dark inspired)
# ---------------------------------------------------------------------------
C_BG      = "#0d1117"
C_SURFACE = "#161b22"
C_CARD    = "#21262d"
C_BORDER  = "#30363d"
C_ACCENT  = "#00d4aa"
C_BLUE    = "#388bfd"
C_PURPLE  = "#8957e5"
C_TEXT    = "#e6edf3"
C_MUTED   = "#8b949e"
C_OK      = "#3fb950"
C_WARN    = "#d29922"
C_ERR     = "#f85149"

STATUS_COLORS = {
    "OK":      C_OK,   "WARN":    C_WARN,
    "ERROR":   C_ERR,  "ABORTED": C_ERR,
    "RUNNING": C_BLUE, "IDLE":    C_MUTED,
}
STATUS_TEXT_COLORS = {
    "OK":      "#000000", "WARN":    "#000000",
    "ERROR":   C_TEXT,    "ABORTED": C_TEXT,
    "RUNNING": "#000000", "IDLE":    C_MUTED,
}

# ---------------------------------------------------------------------------
# Diagnostic helpers (available before the GUI starts)
# ---------------------------------------------------------------------------
_STARTUP_WARNINGS: List[str] = []


def diagnostic(message: str):
    """Record a recoverable problem before or after the GUI is available."""
    text = str(message)
    _STARTUP_WARNINGS.append(text)
    try:
        print(APP_NAME + ": " + text, file=sys.stderr)
    except Exception:
        pass
