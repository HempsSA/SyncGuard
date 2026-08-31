#!/usr/bin/env pythonw
"""
SyncGuard - FreeFileSync Job Manager (GUI only, no console window)
===================================================================
Double-click this file to launch SyncGuard without a black terminal window.
Uses pythonw.exe which suppresses the console.

If you need console output for debugging, run syncguard_protected.py instead.
"""

import sys
import os

# Ensure the project root is on the path
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

from syncguard.__main__ import main

if __name__ == "__main__":
    main()
