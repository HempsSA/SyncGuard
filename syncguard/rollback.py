#!/usr/bin/env python3
"""
SyncGuard Rollback CLI — restore destination from a pre-sync snapshot.

Usage:
    python -m syncguard.rollback --list --job "Job 1"
    python -m syncguard.rollback --job "Job 1" --latest
    python -m syncguard.rollback --job "Job 1" --timestamp 2026-08-31_143000
"""

import argparse
import sys
from pathlib import Path

# Ensure package is importable when run directly
_here = Path(__file__).resolve().parent.parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from syncguard.snapshot import (
    list_snapshots, load_snapshot, compare_snapshots,
    rollback_from_snapshot, capture_manifest,
)
from syncguard.persistence import JobStore


def _log(msg, level="INFO"):
    prefix = {"INFO": "[i]", "WARN": "[!]", "ERROR": "[X]", "OK": "[✓]"}
    print("  {} {}".format(prefix.get(level, "  "), msg))


def main():
    parser = argparse.ArgumentParser(
        description="SyncGuard Rollback — restore destination from snapshot")
    parser.add_argument("--job", required=True,
                        help="Job name to rollback")
    parser.add_argument("--list", action="store_true",
                        help="List available snapshots")
    parser.add_argument("--latest", action="store_true",
                        help="Use the most recent snapshot")
    parser.add_argument("--timestamp", "-t",
                        help="Use a specific snapshot timestamp")
    parser.add_argument("--dry-run", action="store_true",
                        help="Analyze only, don't modify files")
    args = parser.parse_args()

    # Find the job
    store = JobStore()
    job = None
    for j in store.jobs:
        if j.name == args.job:
            job = j
            break

    if job is None:
        print("Job '{}' not found.".format(args.job))
        print("Available jobs:")
        for j in store.jobs:
            print("  - " + j.name)
        sys.exit(1)

    # List snapshots
    if args.list:
        stamps = list_snapshots(job.job_id)
        if not stamps:
            print("No snapshots found for '{}'.".format(job.name))
            sys.exit(0)
        print("Snapshots for '{}':".format(job.name))
        for s in stamps:
            snap = load_snapshot(job.job_id, s)
            if snap:
                print("  {}  |  {} files  |  {}".format(
                    s, snap.total_files, snap.dest_path))
        sys.exit(0)

    # Select snapshot
    stamps = list_snapshots(job.job_id)
    if not stamps:
        print("No snapshots found for '{}'.".format(job.name))
        sys.exit(1)

    if args.latest:
        ts = stamps[-1]
    elif args.timestamp:
        ts = args.timestamp
        if ts not in stamps:
            print("Snapshot '{}' not found.".format(ts))
            print("Available: {}".format(", ".join(stamps)))
            sys.exit(1)
    else:
        # Interactive selection
        print("Available snapshots for '{}':".format(job.name))
        for i, s in enumerate(stamps):
            snap = load_snapshot(job.job_id, s)
            info = "{} files".format(snap.total_files) if snap else "?"
            print("  [{}] {}  ({})".format(i + 1, s, info))
        try:
            choice = int(input("\nSelect snapshot number: ")) - 1
            ts = stamps[choice]
        except (ValueError, IndexError):
            print("Invalid selection.")
            sys.exit(1)

    # Load snapshot
    snap = load_snapshot(job.job_id, ts)
    if snap is None:
        print("Failed to load snapshot '{}'.".format(ts))
        sys.exit(1)

    dest = snap.dest_path
    if not job.destination_path and not dest:
        print("No destination path configured for this job.")
        sys.exit(1)

    dest = dest or job.destination_path
    print("\nSnapshot: {}".format(ts))
    print("Destination: {}".format(dest))
    print("Files in snapshot: {}".format(snap.total_files))

    # Capture current state for comparison
    current = capture_manifest(dest, job.job_id, log_cb=_log)
    if current is None:
        print("Could not read current destination.")
        sys.exit(1)

    # Compare
    diff = compare_snapshots(snap, current)
    print("\nComparison (snapshot vs current):")
    print("  {}".format(diff.summary))
    if diff.hash_mismatch:
        print("  Hash mismatches (possible corruption):")
        for f in diff.hash_mismatch[:10]:
            print("    - " + f)
    if diff.size_anomaly:
        print("  Size anomalies (possible truncation):")
        for f in diff.size_anomaly[:10]:
            print("    - " + f)

    if args.dry_run:
        print("\nDry run — no changes made.")
        sys.exit(0)

    if not diff.is_clean:
        print("\nAnomalies detected. Run with --dry-run first to review.")
        resp = input("Proceed with rollback analysis? (y/N): ")
        if resp.lower() != "y":
            sys.exit(0)

    # Perform rollback analysis
    missing, anomalies = rollback_from_snapshot(snap, dest, log_cb=_log)
    print("\nRollback analysis complete.")
    print("  Missing files to restore: {}".format(missing))
    print("  Size anomalies detected: {}".format(anomalies))

    if missing > 0:
        print("\nTo restore missing files, use FreeFileSync with the")
        print("original source path as the mirror source.")


if __name__ == "__main__":
    main()
