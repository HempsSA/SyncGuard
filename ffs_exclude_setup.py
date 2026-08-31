#!/usr/bin/env python3
"""
FreeFileSync Exclude Filter Setup
==================================
Adds comprehensive Windows exclusion filters to FreeFileSync
batch files (.ffs_batch) and GUI config files (.ffs_gui).

Usage:
    python ffs_exclude_setup.py                     # Scan current dir recursively
    python ffs_exclude_setup.py "C:\\MyBackups"     # Scan specific folder
    python ffs_exclude_setup.py --create template   # Create a template batch file
    python ffs_exclude_setup.py --dry-run "D:\\Sync" # Preview changes without writing

The script will:
  1. Find all .ffs_batch and .ffs_gui files
  2. Read each file's XML
  3. Merge the standard Windows exclude filters into <Filter><Exclude>
  4. Write back only if changes were made
"""

import os
import sys
import re
import xml.etree.ElementTree as ET
from pathlib import Path


# ---------------------------------------------------------------------------
# Standard Windows exclude filters (FreeFileSync format)
# ---------------------------------------------------------------------------

WINDOWS_EXCLUDES = [
    # System / OS Folders
    "\\System Volume Information\\",
    "\\$Recycle.Bin\\",
    "\\RECYCLER\\",
    "\\RECYCLE?\\",
    "\\Recovery\\",
    "\\WinSxS\\",
    "\\SoftwareDistribution\\",
    "\\Installer\\",
    "\\Windows\\Temp\\",
    "\\Windows\\Prefetch\\",
    "\\Windows\\Installer\\$PatchCache$\\",

    # Windows Thumbnail / Explorer Cache
    "*\\thumbs.db",
    "*\\desktop.ini",

    # Windows Temp / Cache Files
    "*.tmp",
    "*.temp",
    "~$*",
    "~*.*",
    "*\\msdownld.tmp",

    # Windows Log / Diagnostic Files
    "*.log",
    "*.etl",
    "*.evtx",

    # Shortcut Files
    "*.lnk",

    # Version Control
    "\\.git\\",
    "\\.svn\\",
    "\\.hg\\",

    # macOS Artifacts (cross-platform sync)
    "*.DS_Store",
    "\\._*",
    "\\__MACOSX\\",

    # Linux Artifacts (cross-platform sync)
    ".Trash-*",
    ".cache\\",

    # Browser Caches
    "*\\Cache\\",
    "*\\cache2\\",
    "*\\Service Worker\\",

    # SyncGuard Internal
    "\\syncguard_cache\\",
]


# ---------------------------------------------------------------------------
# XML manipulation helpers
# ---------------------------------------------------------------------------

def _indent_xml(elem, level=0):
    """Add pretty-print indentation to an XML element tree."""
    indent = "\n" + "  " * level
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = indent + "  "
        if not elem.tail or not elem.tail.strip():
            elem.tail = indent
        for child in elem:
            _indent_xml(child, level + 1)
        if not child.tail or not child.tail.strip():
            child.tail = indent
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = indent
    if not level:
        elem.tail = "\n"


def _read_xml_text(filepath):
    """Read file as text, preserving the XML declaration encoding."""
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()


def _get_existing_excludes(root):
    """Extract the current list of <Item> entries from <Filter><Exclude>."""
    excludes = []
    for filt in root.iter("Filter"):
        for exc in filt.iter("Exclude"):
            for item in exc.findall("Item"):
                val = (item.text or "").strip()
                if val:
                    excludes.append(val)
    return excludes


def _merge_excludes(existing, additions):
    """Merge new exclusions into existing, avoiding duplicates (case-insensitive)."""
    seen = {e.lower() for e in existing}
    merged = list(existing)
    added = 0
    for pattern in additions:
        key = pattern.lower()
        if key not in seen:
            merged.append(pattern)
            seen.add(key)
            added += 1
    return merged, added


def _apply_excludes_to_xml(root, merged_excludes):
    """Write the merged exclude list back into the XML tree."""
    # Find or create <Filter>
    filt = root.find("Filter")
    if filt is None:
        filt = ET.SubElement(root, "Filter")
        # Move <Filter> to the right position (after <Synchronize>)
        children = list(root)
        target_idx = 0
        for i, child in enumerate(children):
            if child.tag in ("Synchronize", "Compare"):
                target_idx = i + 1
        root.remove(filt)
        root.insert(target_idx, filt)

    # Find or create <Exclude>
    exc = filt.find("Exclude")
    if exc is None:
        exc = ET.SubElement(filt, "Exclude")
        # Move <Exclude> after <Include>
        inc = filt.find("Include")
        if inc is not None:
            filt.remove(exc)
            children = list(filt)
            idx = children.index(inc) + 1
            filt.insert(idx, exc)

    # Clear existing items
    for item in exc.findall("Item"):
        exc.remove(item)

    # Add merged items
    for pattern in merged_excludes:
        item = ET.SubElement(exc, "Item")
        item.text = pattern


def _get_xml_format_version(text):
    """Extract XmlFormat version from the raw XML text."""
    m = re.search(r'XmlFormat="(\d+)"', text)
    return m.group(1) if m else "23"


def _xml_escape(s):
    """Escape special XML characters."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_file(filepath, dry_run=False):
    """Process a single .ffs_batch or .ffs_gui file. Returns (changed, added_count)."""
    text = _read_xml_text(filepath)

    # Parse XML
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        print(f"  SKIP (parse error): {e}")
        return False, 0

    # Get existing excludes
    existing = _get_existing_excludes(root)

    # Merge
    merged, added = _merge_excludes(existing, WINDOWS_EXCLUDES)

    if added == 0:
        return False, 0

    # Apply back
    _apply_excludes_to_xml(root, merged)

    # Pretty-print
    _indent_xml(root)

    # Serialize
    version = _get_xml_format_version(text)
    new_text = ET.tostring(root, encoding="unicode", xml_declaration=False)
    output = '<?xml version="1.0" encoding="utf-8"?>\n' + new_text

    if not dry_run:
        # Backup original
        backup = filepath + ".bak"
        if not os.path.exists(backup):
            with open(filepath, "r", encoding="utf-8") as f:
                with open(backup, "w", encoding="utf-8") as fb:
                    fb.write(f.read())

        with open(filepath, "w", encoding="utf-8", newline="\n") as f:
            f.write(output)

    return True, added


def create_template_batch(filepath, source, target, dry_run=False):
    """Create a new FreeFileSync batch file with exclude filters pre-configured."""
    items = "\n".join(
        f"      <Item>{_xml_escape(p)}</Item>" for p in WINDOWS_EXCLUDES
    )

    xml = f'''<?xml version="1.0" encoding="utf-8"?>
<FreeFileSync XmlType="BATCH" XmlFormat="23">
  <Notes>Auto-generated with Windows exclude filters</Notes>
  <Compare>
    <Variant>TimeAndSize</Variant>
    <Symlinks>Exclude</Symlinks>
    <IgnoreTimeShift/>
  </Compare>
  <Synchronize>
    <Changes>
      <Left Create="right" Update="right" Delete="none"/>
      <Right Create="none" Update="right" Delete="none"/>
    </Changes>
    <DeletionPolicy>RecycleBin</DeletionPolicy>
    <VersioningFolder Style="Replace"/>
  </Synchronize>
  <Filter>
    <Include>
      <Item>*</Item>
    </Include>
    <Exclude>
{items}
    </Exclude>
    <SizeMin Unit="None">0</SizeMin>
    <SizeMax Unit="None">0</SizeMax>
    <TimeSpan Type="None">0</TimeSpan>
  </Filter>
  <FolderPairs>
    <Pair>
      <Left Threads="4">{_xml_escape(source)}</Left>
      <Right Threads="4">{_xml_escape(target)}</Right>
    </Pair>
  </FolderPairs>
  <Errors Ignore="true" Retry="2" Delay="1"/>
  <PostSyncCommand Condition="Completion"/>
  <LogFolder></LogFolder>
  <EmailNotification Condition="Never"/>
  <GridViewType>Action</GridViewType>
  <Batch>
    <ProgressDialog Minimized="true" AutoClose="true"/>
    <ErrorDialog>Show</ErrorDialog>
    <PostSyncAction>None</PostSyncAction>
  </Batch>
</FreeFileSync>'''

    if not dry_run:
        with open(filepath, "w", encoding="utf-8", newline="\n") as f:
            f.write(xml)

    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    dry_run = "--dry-run" in sys.argv
    create_mode = "--create" in sys.argv

    # Filter out flags to get the path
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    print("=" * 60)
    print("  FreeFileSync Exclude Filter Setup")
    print("=" * 60)

    if dry_run:
        print("  [DRY RUN] No files will be modified.\n")

    # -- Create mode --
    if create_mode:
        if not args:
            print("Usage: python ffs_exclude_setup.py --create <output.ffs_batch>")
            print("       python ffs_exclude_setup.py --create <output.ffs_batch> <source> <target>")
            sys.exit(1)

        output = args[0]
        source = args[1] if len(args) > 1 else "C:\\Source"
        target = args[2] if len(args) > 2 else "D:\\Target"

        if dry_run:
            print(f"Would create: {output}")
            print(f"  Source: {source}")
            print(f"  Target: {target}")
            print(f"  Excludes: {len(WINDOWS_EXCLUDES)} patterns")
        else:
            create_template_batch(output, source, target)
            print(f"Created: {output}")
            print(f"  Source: {source}")
            print(f"  Target: {target}")
            print(f"  Excludes: {len(WINDOWS_EXCLUDES)} patterns applied")
        return

    # -- Scan mode --
    scan_dir = args[0] if args else "."
    scan_dir = os.path.abspath(scan_dir)

    print(f"Scanning: {scan_dir}")
    print(f"Patterns: {len(WINDOWS_EXCLUDES)} exclude filters")
    print()

    # Find all FreeFileSync config files
    ffs_files = []
    for root_dir, dirs, files in os.walk(scan_dir):
        for f in files:
            if f.lower().endswith((".ffs_batch", ".ffs_gui")):
                ffs_files.append(os.path.join(root_dir, f))

    if not ffs_files:
        print("No .ffs_batch or .ffs_gui files found.")
        print()
        print("Tip: Create a template with:")
        print(f'  python {os.path.basename(__file__)} --create template.ffs_batch "C:\\Source" "D:\\Target"')
        return

    print(f"Found {len(ffs_files)} FreeFileSync config file(s):")
    print()

    total_added = 0
    total_changed = 0

    for ffs_path in sorted(ffs_files):
        rel = os.path.relpath(ffs_path, scan_dir)
        print(f"  {rel}")

        changed, added = process_file(ffs_path, dry_run=dry_run)

        if changed:
            total_changed += 1
            total_added += added
            print(f"    + {added} new filter(s) added")
        else:
            print(f"    = already up to date")
        print()

    # Summary
    print("-" * 60)
    if dry_run:
        print(f"  DRY RUN: {total_changed} file(s) would be modified,")
        print(f"           {total_added} new filter(s) total.")
    else:
        print(f"  Done: {total_changed} file(s) modified,")
        print(f"        {total_added} new filter(s) added.")
        if total_changed > 0:
            print()
            print("  Backups saved as .bak files.")
            print("  Open FreeFileSync to verify the changes.")

    print("-" * 60)

    # Show the filters that were added
    if total_added > 0:
        print()
        print("  Filters applied:")
        for p in WINDOWS_EXCLUDES:
            print(f"    {p}")


if __name__ == "__main__":
    main()
