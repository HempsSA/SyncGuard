#!/usr/bin/env python3
"""
SyncGuard - FreeFileSync Job Manager
====================================
Dark-mode GUI for managing and scheduling multiple FreeFileSync jobs
with automatic change-rate detection and abort protection.

This is a backward-compatible launcher.
The actual implementation lives in the syncguard/ package.

Usage:
    python syncguard_protected.py
    python -m syncguard
"""

import sys
import os

# Ensure the project root is on the path so ``import syncguard`` works
# regardless of how the script is invoked.
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

from syncguard.__main__ import main

if __name__ == "__main__":
    main()
