#!/usr/bin/env python3
"""
FreeFileSync Exclude Filter Setup
==================================
Adds comprehensive Windows exclusion filters to FreeFileSync
batch files (.ffs_batch) and GUI config files (.ffs_gui).

Usage (GUI):
    python ffs_exclude_setup.py                  # Launch the GUI

Usage (CLI):
    python ffs_exclude_setup.py --cli            # Scan current dir
    python ffs_exclude_setup.py --cli "C:\\Backups"  # Scan specific dir
    python ffs_exclude_setup.py --cli --create template.ffs_batch  # Create template
    python ffs_exclude_setup.py --cli --dry-run  # Preview only
"""

import os
import sys
import re
import threading
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
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()


def _get_existing_excludes(root):
    excludes = []
    for filt in root.iter("Filter"):
        for exc in filt.iter("Exclude"):
            for item in exc.findall("Item"):
                val = (item.text or "").strip()
                if val:
                    excludes.append(val)
    return excludes


def _merge_excludes(existing, additions):
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
    filt = root.find("Filter")
    if filt is None:
        filt = ET.SubElement(root, "Filter")
        children = list(root)
        target_idx = 0
        for i, child in enumerate(children):
            if child.tag in ("Synchronize", "Compare"):
                target_idx = i + 1
        root.remove(filt)
        root.insert(target_idx, filt)

    exc = filt.find("Exclude")
    if exc is None:
        exc = ET.SubElement(filt, "Exclude")
        inc = filt.find("Include")
        if inc is not None:
            filt.remove(exc)
            children = list(filt)
            idx = children.index(inc) + 1
            filt.insert(idx, exc)

    for item in exc.findall("Item"):
        exc.remove(item)

    for pattern in merged_excludes:
        item = ET.SubElement(exc, "Item")
        item.text = pattern


def _get_xml_format_version(text):
    m = re.search(r'XmlFormat="(\d+)"', text)
    return m.group(1) if m else "23"


def _xml_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_file(filepath, filters=None, dry_run=False):
    """Process a single file. Returns (changed, added_count, merged_list)."""
    if filters is None:
        filters = WINDOWS_EXCLUDES

    text = _read_xml_text(filepath)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        return False, 0, [], str(e)

    existing = _get_existing_excludes(root)
    merged, added = _merge_excludes(existing, filters)

    if added == 0:
        return False, 0, merged, None

    _apply_excludes_to_xml(root, merged)
    _indent_xml(root)

    new_text = ET.tostring(root, encoding="unicode", xml_declaration=False)
    output = '<?xml version="1.0" encoding="utf-8"?>\n' + new_text

    if not dry_run:
        backup = filepath + ".bak"
        if not os.path.exists(backup):
            with open(filepath, "r", encoding="utf-8") as f:
                with open(backup, "w", encoding="utf-8") as fb:
                    fb.write(f.read())
        with open(filepath, "w", encoding="utf-8", newline="\n") as f:
            f.write(output)

    return True, added, merged, None


def create_template_batch(filepath, source, target, filters=None, dry_run=False):
    if filters is None:
        filters = WINDOWS_EXCLUDES

    items = "\n".join(
        f"      <Item>{_xml_escape(p)}</Item>" for p in filters
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
# GUI
# ---------------------------------------------------------------------------

def launch_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    # -- Dark theme colours --
    C_BG      = "#1a1b26"
    C_SURFACE = "#24283b"
    C_CARD    = "#2f3347"
    C_BORDER  = "#414868"
    C_ACCENT  = "#00d2a0"
    C_BLUE    = "#388bfd"
    C_TEXT    = "#c0caf5"
    C_MUTED   = "#565f89"
    C_OK      = "#3fb950"
    C_WARN    = "#d29b2a"
    C_ERR     = "#f85149"

    root = tk.Tk()
    root.title("FreeFileSync Exclude Filter Setup")
    root.configure(bg=C_BG)
    root.minsize(600, 400)

    # Centre on screen — use 80% of screen size
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    w = min(860, int(sw * 0.8))
    h = min(680, int(sh * 0.8))
    root.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("TFrame", background=C_BG)
    style.configure("Card.TFrame", background=C_CARD)
    style.configure("TLabel", background=C_BG, foreground=C_TEXT,
                     font=("Segoe UI", 11))
    style.configure("Header.TLabel", background=C_BG, foreground=C_ACCENT,
                     font=("Segoe UI", 16, "bold"))
    style.configure("Sub.TLabel", background=C_BG, foreground=C_MUTED,
                     font=("Segoe UI", 10))
    style.configure("Card.TLabel", background=C_CARD, foreground=C_TEXT,
                     font=("Segoe UI", 10))
    style.configure("Accent.TButton", background=C_ACCENT, foreground="#000",
                     font=("Segoe UI", 11, "bold"), padding=(16, 8))
    style.map("Accent.TButton",
              background=[("active", "#00b88c"), ("disabled", C_BORDER)])
    style.configure("Blue.TButton", background=C_BLUE, foreground="#fff",
                     font=("Segoe UI", 11, "bold"), padding=(16, 8))
    style.map("Blue.TButton",
              background=[("active", "#2978e0"), ("disabled", C_BORDER)])
    style.configure("Ghost.TButton", background=C_CARD, foreground=C_TEXT,
                     font=("Segoe UI", 10), padding=(10, 6))
    style.map("Ghost.TButton",
              background=[("active", C_BORDER)])
    style.configure("TCheckbutton", background=C_CARD, foreground=C_TEXT,
                     font=("Segoe UI", 10))
    style.configure("TEntry", fieldbackground=C_CARD, foreground=C_TEXT,
                     insertcolor=C_TEXT)
    style.configure("Treeview", background=C_CARD, foreground=C_TEXT,
                     fieldbackground=C_CARD, font=("Consolas", 10),
                     rowheight=24, borderwidth=0)
    style.configure("Treeview.Heading", background=C_SURFACE, foreground=C_MUTED,
                     font=("Segoe UI", 10, "bold"), borderwidth=0)
    style.map("Treeview", background=[("selected", C_BORDER)],
              foreground=[("selected", C_ACCENT)])

    # -- State --
    selected_files = []
    custom_filters = list(WINDOWS_EXCLUDES)

    # ── Fixed bottom bar (always visible) ────────────────────────
    btn_frame = ttk.Frame(root)
    btn_frame.pack(fill="x", side="bottom", padx=20, pady=(4, 16))

    # -- Status bar --
    status_frame = ttk.Frame(root)
    status_frame.pack(fill="x", side="bottom")
    ttk.Label(status_frame,
              text="Select .ffs_batch or .ffs_gui files, edit filters, then click Apply",
              style="Sub.TLabel").pack(padx=20, pady=(0, 4), anchor="w")

    # ── Scrollable content area ──────────────────────────────────
    canvas = tk.Canvas(root, bg=C_BG, highlightthickness=0)
    scrollbar = ttk.Scrollbar(root, orient="vertical", command=canvas.yview)
    scroll_frame = ttk.Frame(canvas)

    scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas_window = canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)

    # Make scroll_frame width match canvas width
    def _on_canvas_configure(event):
        canvas.itemconfig(canvas_window, width=event.width)
    canvas.bind("<Configure>", _on_canvas_configure)

    # Mouse wheel scrolling
    def _on_mousewheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    canvas.bind_all("<MouseWheel>", _on_mousewheel)

    scrollbar.pack(side="right", fill="y")
    canvas.pack(side="top", fill="both", expand=True)

    # -- Header inside scroll area --
    header = ttk.Frame(scroll_frame)
    header.pack(fill="x", padx=20, pady=(16, 4))
    ttk.Label(header, text="FreeFileSync Exclude Setup",
              style="Header.TLabel").pack(side="left")
    ttk.Label(header, text=f"{len(WINDOWS_EXCLUDES)} filters",
              style="Sub.TLabel").pack(side="right", pady=(6, 0))

    # -- File selection area --
    file_frame = ttk.Frame(scroll_frame)
    file_frame.pack(fill="x", padx=20, pady=(8, 4))

    btn_row = ttk.Frame(file_frame)
    btn_row.pack(fill="x")

    def _browse_files():
        paths = filedialog.askopenfilenames(
            title="Select FreeFileSync config files",
            filetypes=[
                ("FreeFileSync configs", "*.ffs_batch *.ffs_gui"),
                ("Batch files", "*.ffs_batch"),
                ("GUI configs", "*.ffs_gui"),
                ("All files", "*.*"),
            ])
        for p in paths:
            if p not in selected_files:
                selected_files.append(p)
        _refresh_file_list()

    def _browse_folder():
        folder = filedialog.askdirectory(title="Scan folder for FreeFileSync configs")
        if not folder:
            return
        count = 0
        for dirpath, _, filenames in os.walk(folder):
            for f in filenames:
                if f.lower().endswith((".ffs_batch", ".ffs_gui")):
                    fp = os.path.join(dirpath, f)
                    if fp not in selected_files:
                        selected_files.append(fp)
                        count += 1
        if count == 0:
            messagebox.showinfo("No files found",
                                f"No .ffs_batch or .ffs_gui files found in:\n{folder}")
        _refresh_file_list()

    def _clear_files():
        selected_files.clear()
        _refresh_file_list()

    ttk.Button(btn_row, text="Browse Files...", style="Ghost.TButton",
               command=_browse_files).pack(side="left", padx=(0, 6))
    ttk.Button(btn_row, text="Scan Folder...", style="Ghost.TButton",
               command=_browse_folder).pack(side="left", padx=(0, 6))
    ttk.Button(btn_row, text="Clear", style="Ghost.TButton",
               command=_clear_files).pack(side="left")

    # File list
    file_tree_frame = tk.Frame(file_frame, bg=C_CARD, highlightbackground=C_BORDER,
                                highlightthickness=1)
    file_tree_frame.pack(fill="x", pady=(8, 0))

    file_tree = ttk.Treeview(file_tree_frame, columns=("path", "status"),
                              show="headings", height=4, selectmode="none")
    file_tree.heading("path", text="File Path", anchor="w")
    file_tree.heading("status", text="Status", anchor="w")
    file_tree.column("path", width=600, minwidth=300)
    file_tree.column("status", width=140, minwidth=100, anchor="center")

    scrollbar_tree = ttk.Scrollbar(file_tree_frame, orient="vertical",
                               command=file_tree.yview)
    file_tree.configure(yscrollcommand=scrollbar_tree.set)
    file_tree.pack(side="left", fill="both", expand=True)
    scrollbar_tree.pack(side="right", fill="y")

    def _refresh_file_list():
        for item in file_tree.get_children():
            file_tree.delete(item)
        for fp in selected_files:
            file_tree.insert("", "end", values=(fp, "Pending"))
        file_count_label.configure(text=f"{len(selected_files)} file(s) selected")

    file_count_label = ttk.Label(file_frame, text="0 file(s) selected",
                                  style="Sub.TLabel")
    file_count_label.pack(anchor="w", pady=(4, 0))

    # -- Filter editor --
    filter_frame = ttk.Frame(scroll_frame)
    filter_frame.pack(fill="both", padx=20, pady=(4, 4))

    ttk.Label(filter_frame, text="Exclude Filters",
              font=("Segoe UI", 11, "bold"), foreground=C_TEXT,
              background=C_BG).pack(anchor="w")

    ttk.Label(filter_frame,
              text="Edit the filter list below. One pattern per line. "
                   "These will be merged into each selected file.",
              style="Sub.TLabel").pack(anchor="w", pady=(0, 4))

    text_frame = tk.Frame(filter_frame, bg=C_CARD,
                           highlightbackground=C_BORDER, highlightthickness=1)
    text_frame.pack(fill="both", expand=True)

    filter_text = tk.Text(text_frame, bg=C_CARD, fg=C_TEXT,
                           insertbackground=C_TEXT, font=("Consolas", 10),
                           wrap="none", borderwidth=0, padx=8, pady=8,
                           selectbackground=C_BORDER, selectforeground=C_ACCENT)
    filter_scroll_y = ttk.Scrollbar(text_frame, orient="vertical",
                                     command=filter_text.yview)
    filter_scroll_x = ttk.Scrollbar(text_frame, orient="horizontal",
                                     command=filter_text.xview)
    filter_text.configure(yscrollcommand=filter_scroll_y.set,
                          xscrollcommand=filter_scroll_x.set)
    filter_scroll_y.pack(side="right", fill="y")
    filter_scroll_x.pack(side="bottom", fill="x")
    filter_text.pack(side="left", fill="both", expand=True)

    # Populate filters
    filter_text.insert("1.0", "\n".join(WINDOWS_EXCLUDES))

    def _reset_filters():
        custom_filters.clear()
        custom_filters.extend(WINDOWS_EXCLUDES)
        filter_text.delete("1.0", "end")
        filter_text.insert("1.0", "\n".join(WINDOWS_EXCLUDES))

    def _get_current_filters():
        raw = filter_text.get("1.0", "end").strip()
        return [line.strip() for line in raw.splitlines() if line.strip()]

    ttk.Button(filter_frame, text="Reset to Defaults",
               style="Ghost.TButton", command=_reset_filters).pack(
                   anchor="w", pady=(4, 0))

    # -- Log area --
    log_frame = ttk.Frame(scroll_frame)
    log_frame.pack(fill="x", padx=20, pady=(4, 4))

    ttk.Label(log_frame, text="Output",
              font=("Segoe UI", 11, "bold"), foreground=C_TEXT,
              background=C_BG).pack(anchor="w")

    log_box = tk.Text(log_frame, bg=C_SURFACE, fg=C_MUTED,
                       font=("Consolas", 9), height=6, wrap="word",
                       borderwidth=0, state="disabled", padx=8, pady=6,
                       highlightbackground=C_BORDER, highlightthickness=1)
    log_scroll = ttk.Scrollbar(log_frame, orient="vertical",
                                command=log_box.yview)
    log_box.configure(yscrollcommand=log_scroll.set)
    log_scroll.pack(side="right", fill="y")
    log_box.pack(fill="x")

    def _log(msg, tag="info"):
        log_box.configure(state="normal")
        log_box.insert("end", msg + "\n", tag)
        log_box.see("end")
        log_box.configure(state="disabled")

    log_box.tag_configure("ok", foreground=C_OK)
    log_box.tag_configure("warn", foreground=C_WARN)
    log_box.tag_configure("err", foreground=C_ERR)
    log_box.tag_configure("info", foreground=C_MUTED)

    def _apply_filters(dry_run=False):
        files = list(selected_files)
        if not files:
            messagebox.showwarning("No files", "Select at least one FreeFileSync config file.")
            return

        filters = _get_current_filters()
        if not filters:
            messagebox.showwarning("No filters", "The filter list is empty.")
            return

        tag = "[DRY RUN] " if dry_run else ""
        _log(f"{'=' * 50}", "info")
        _log(f"{tag}Processing {len(files)} file(s) with {len(filters)} filters...", "info")

        total_added = 0
        total_changed = 0

        for fp in files:
            name = os.path.basename(fp)
            try:
                changed, added, merged, err = process_file(fp, filters, dry_run=dry_run)
            except Exception as e:
                _log(f"  ERROR: {name} - {e}", "err")
                # Update tree status
                for item in file_tree.get_children():
                    vals = file_tree.item(item, "values")
                    if vals[0] == fp:
                        file_tree.item(item, values=(fp, "ERROR"))
                continue

            if err:
                _log(f"  SKIP: {name} - {err}", "warn")
                for item in file_tree.get_children():
                    vals = file_tree.item(item, "values")
                    if vals[0] == fp:
                        file_tree.item(item, values=(fp, "SKIPPED"))
            elif changed:
                total_changed += 1
                total_added += added
                _log(f"  OK: {name} - +{added} new filter(s)", "ok")
                for item in file_tree.get_children():
                    vals = file_tree.item(item, "values")
                    if vals[0] == fp:
                        file_tree.item(item, values=(fp, f"+{added} added"))
            else:
                _log(f"  {name} - already up to date", "info")
                for item in file_tree.get_children():
                    vals = file_tree.item(item, "values")
                    if vals[0] == fp:
                        file_tree.item(item, values=(fp, "Up to date"))

        _log(f"{'-' * 50}", "info")
        if dry_run:
            _log(f"DRY RUN: {total_changed} file(s) would be modified, "
                 f"{total_added} new filter(s) total.", "warn")
        else:
            _log(f"Done: {total_changed} file(s) modified, "
                 f"{total_added} new filter(s) added.", "ok")
            if total_changed > 0:
                _log("Backups saved as .bak files.", "info")
        _log("", "info")

    def _apply_now():
        _apply_filters(dry_run=False)

    def _preview():
        _apply_filters(dry_run=True)

    ttk.Button(btn_frame, text="Preview (Dry Run)", style="Blue.TButton",
               command=_preview).pack(side="left", padx=(0, 8))
    ttk.Button(btn_frame, text="Apply Filters", style="Accent.TButton",
               command=_apply_now).pack(side="left")

    root.mainloop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def launch_cli():
    dry_run = "--dry-run" in sys.argv
    create_mode = "--create" in sys.argv
    args = [a for a in sys.argv[1:]
            if not a.startswith("--")]

    print("=" * 60)
    print("  FreeFileSync Exclude Filter Setup (CLI)")
    print("=" * 60)

    if dry_run:
        print("  [DRY RUN] No files will be modified.\n")

    if create_mode:
        if not args:
            print("Usage: python ffs_exclude_setup.py --cli --create <output.ffs_batch> <src> <dst>")
            sys.exit(1)
        output = args[0]
        source = args[1] if len(args) > 1 else "C:\\Source"
        target = args[2] if len(args) > 2 else "D:\\Target"
        create_template_batch(output, source, target, dry_run=dry_run)
        print(f"Created: {output}")
        print(f"  Source: {source}")
        print(f"  Target: {target}")
        print(f"  Excludes: {len(WINDOWS_EXCLUDES)} patterns")
        return

    scan_dir = args[0] if args else "."
    scan_dir = os.path.abspath(scan_dir)
    print(f"Scanning: {scan_dir}")
    print(f"Patterns: {len(WINDOWS_EXCLUDES)} exclude filters\n")

    ffs_files = []
    for root_dir, dirs, files in os.walk(scan_dir):
        for f in files:
            if f.lower().endswith((".ffs_batch", ".ffs_gui")):
                ffs_files.append(os.path.join(root_dir, f))

    if not ffs_files:
        print("No .ffs_batch or .ffs_gui files found.")
        return

    print(f"Found {len(ffs_files)} file(s):\n")
    total_added = 0
    total_changed = 0

    for ffs_path in sorted(ffs_files):
        rel = os.path.relpath(ffs_path, scan_dir)
        print(f"  {rel}")
        changed, added, _, err = process_file(ffs_path, dry_run=dry_run)
        if err:
            print(f"    ERROR: {err}")
        elif changed:
            total_changed += 1
            total_added += added
            print(f"    + {added} new filter(s)")
        else:
            print(f"    = up to date")
        print()

    print("-" * 60)
    print(f"  Done: {total_changed} file(s) modified, {total_added} new filter(s)")
    print("-" * 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    # GUI mode by default; CLI if --cli flag is present
    if "--cli" in sys.argv:
        sys.argv.remove("--cli")
        launch_cli()
    else:
        launch_gui()


if __name__ == "__main__":
    main()
