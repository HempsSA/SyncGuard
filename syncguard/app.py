"""
SyncGuard GUI — dark-mode CustomTkinter application with scan progress,
threshold override dialog, and system tray integration.
"""

import os
import csv
import time
import copy
import uuid
import queue as _queue
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import customtkinter as ctk
import schedule
import pystray
from PIL import Image, ImageDraw

import tkinter as tk
from tkinter import filedialog, messagebox

from .constants import (
    APP_NAME, LOG_MAX,
    C_BG, C_SURFACE, C_CARD, C_BORDER, C_ACCENT, C_BLUE,
    C_TEXT, C_MUTED, C_OK, C_WARN, C_ERR,
    STATUS_COLORS, STATUS_TEXT_COLORS,
    _STARTUP_WARNINGS,
)
from .persistence import (
    ScanCache, ScanHistory, JobConfig, JobStore,
)
from .scanner import (
    ParallelScanner, ChangeGuard, ScanProgress,
    _is_network_path,
)
from .guardian import (
    _GuardianCooldown, _GuardianEventHandler,
    _RenameReverter, _DeletionGuard, _GuardianState,
    _WdObserver,
)

try:
    import psutil as _psutil
    _PSUTIL_OK = True
except ImportError:
    _PSUTIL_OK = False


# ---------------------------------------------------------------------------
# Responsive scaling
# ---------------------------------------------------------------------------

def _screen_info():
    r = tk.Tk()
    r.withdraw()
    sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
    r.destroy()
    if sh <= 800:
        sc = 0.78
    elif sh <= 900:
        sc = 0.86
    elif sh <= 1080:
        sc = 0.93
    else:
        sc = 1.0
    return sw, sh, sc


def _sc(value: int, scale: float) -> int:
    return max(1, int(value * scale))


def _darken(hex_color: str, amount: int = 30) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return "#{:02x}{:02x}{:02x}".format(
        max(0, r - amount), max(0, g - amount), max(0, b - amount))


def _fmt_dur(seconds: float) -> str:
    """Format a duration in seconds to a compact human-readable string."""
    if seconds < 1:
        return "<1s"
    if seconds < 60:
        return str(int(seconds)) + "s"
    m = int(seconds // 60)
    s = int(seconds % 60)
    if m < 60:
        return str(m) + "m " + str(s) + "s"
    h = int(m // 60)
    m = int(m % 60)
    return str(h) + "h " + str(m) + "m"


# ---------------------------------------------------------------------------
# Scheduler service
# ---------------------------------------------------------------------------

class SchedulerService:
    def __init__(self, store: JobStore, log_cb, run_cb):
        self.store    = store
        self.log_cb   = log_cb
        self.run_cb   = run_cb
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock    = threading.Lock()

    def rebuild(self):
        with self._lock:
            schedule.clear()
            count = 0
            for job in self.store.jobs:
                if not job.enabled or not job.schedule_times:
                    continue
                for t in job.schedule_times:
                    def make_task(j):
                        def task():
                            self.run_cb(j, "schedule")
                        return task
                    schedule.every().day.at(t).do(make_task(job))
                    count += 1
            self.log_cb("Scheduler rebuilt: " + str(count) + " task(s) registered.")

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            try:
                with self._lock:
                    schedule.run_pending()
            except Exception as exc:
                self.log_cb("Scheduler loop error: " + str(exc))
            time.sleep(30)


# ---------------------------------------------------------------------------
# Styled widget helpers
# ---------------------------------------------------------------------------

def _label(parent, text, size=12, weight="normal", color=C_TEXT, **kw):
    return ctk.CTkLabel(
        parent, text=text, font=(None, size, weight),
        text_color=color, **kw)


def _entry(parent, **kw):
    return ctk.CTkEntry(
        parent, fg_color=C_CARD, border_color=C_BORDER,
        border_width=1, text_color=C_TEXT, **kw)


def _btn(parent, text, command, color=C_ACCENT, text_color=None, **kw):
    tc = text_color or (
        "#000000" if color in (C_ACCENT, C_OK, C_WARN) else C_TEXT)
    return ctk.CTkButton(
        parent, text=text, command=command,
        fg_color=color, hover_color=_darken(color),
        text_color=tc, **kw)


# ---------------------------------------------------------------------------
# Job row (sidebar)
# ---------------------------------------------------------------------------

class JobRow(ctk.CTkFrame):
    def __init__(self, parent, job: JobConfig, on_select, index: int,
                 s: float, **kw):
        super().__init__(parent, fg_color="transparent",
                         corner_radius=6, **kw)
        self.index = index
        self.on_select = on_select
        self.configure(cursor="hand2")

        self.dot = ctk.CTkLabel(
            self, text="", width=_sc(10, s), height=_sc(10, s),
            fg_color=C_MUTED, corner_radius=5)
        self.dot.grid(row=0, column=0,
                      padx=(_sc(8, s), _sc(5, s)), pady=_sc(7, s))

        self.name_lbl = ctk.CTkLabel(
            self, text=job.name, font=(None, _sc(13, s)),
            text_color=C_TEXT, anchor="w")
        self.name_lbl.grid(row=0, column=1, sticky="w",
                           padx=2, pady=_sc(7, s))

        sched = (str(len(job.schedule_times)) + "x"
                 if job.schedule_times else "manual")
        self.sched_lbl = ctk.CTkLabel(
            self, text=sched, font=(None, _sc(10, s)),
            text_color=C_MUTED, anchor="e")
        self.sched_lbl.grid(row=0, column=2, sticky="e",
                            padx=(4, _sc(10, s)), pady=_sc(7, s))

        self.columnconfigure(1, weight=1)
        self._ctx_cb = None
        for w in (self, self.dot, self.name_lbl, self.sched_lbl):
            w.bind("<Button-1>", self._click)
            w.bind("<Button-3>", self._right_click)

    def _click(self, _=None):
        self.on_select(self.index)

    def _right_click(self, event):
        self.on_select(self.index)
        menu = tk.Menu(
            self, tearoff=0,
            bg=C_CARD, fg=C_TEXT,
            activebackground=C_ACCENT, activeforeground="#000000",
            bd=1, relief="flat")
        menu.add_command(label="Run Now",
                         command=lambda: self._ctx("run"))
        menu.add_command(label="Duplicate Job",
                         command=lambda: self._ctx("dup"))
        menu.add_separator()
        menu.add_command(label="Remove Job",
                         command=lambda: self._ctx("remove"))
        menu.post(event.x_root, event.y_root)

    def _ctx(self, action: str):
        if self._ctx_cb:
            self._ctx_cb(self.index, action)

    def set_ctx_callback(self, cb):
        self._ctx_cb = cb

    def set_selected(self, v):
        self.configure(fg_color=C_CARD if v else "transparent")

    def set_status(self, s):
        self.dot.configure(fg_color=STATUS_COLORS.get(s, C_MUTED))

    def update_from_job(self, job):
        self.name_lbl.configure(text=job.name)
        t = (str(len(job.schedule_times)) + "x"
             if job.schedule_times else "manual")
        self.sched_lbl.configure(text=t)


# ---------------------------------------------------------------------------
# Scan progress panel
# ---------------------------------------------------------------------------

class ScanProgressPanel(ctk.CTkFrame):
    def __init__(self, parent, s: float, **kw):
        super().__init__(parent, fg_color=C_BG, corner_radius=0, **kw)
        self._s = s
        self._start_time = 0.0
        self._build(s)

    def _build(self, s):
        # Header
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.pack(fill="x", padx=_sc(24, s), pady=(_sc(16, s), 0))

        self._engine_badge = ctk.CTkLabel(
            hdr, text="  PARALLEL  ",
            font=(None, _sc(9, s), "bold"),
            fg_color=C_BORDER, corner_radius=4, text_color=C_MUTED)
        self._engine_badge.pack(side="left")

        self._job_lbl = _label(hdr, "", size=_sc(15, s), weight="bold")
        self._job_lbl.pack(side="left", padx=_sc(10, s))

        self._elapsed_lbl = _label(hdr, "", size=_sc(10, s), color=C_MUTED)
        self._elapsed_lbl.pack(side="right")

        # Progress bar
        bar_frame = ctk.CTkFrame(self, fg_color="transparent")
        bar_frame.pack(fill="x", padx=_sc(24, s), pady=(_sc(12, s), 4))

        self._bar = ctk.CTkProgressBar(
            bar_frame, fg_color=C_CARD, progress_color=C_ACCENT,
            height=_sc(12, s), corner_radius=6)
        self._bar.pack(fill="x")
        self._bar.set(0)
        self._bar.configure(mode="indeterminate")
        self._bar.start()

        pct_row = ctk.CTkFrame(bar_frame, fg_color="transparent")
        pct_row.pack(fill="x", pady=(4, 0))
        self._pct_lbl = _label(pct_row, "Scanning...",
                               size=_sc(10, s), color=C_MUTED)
        self._pct_lbl.pack(side="left")
        self._eta_lbl = _label(pct_row, "", size=_sc(10, s), color=C_MUTED)
        self._eta_lbl.pack(side="right")

        # Stat cards
        cards = ctk.CTkFrame(self, fg_color="transparent")
        cards.pack(pady=_sc(14, s))

        def stat_card(label, attr_val, color=C_ACCENT):
            card = ctk.CTkFrame(cards, fg_color=C_CARD, corner_radius=10)
            card.pack(side="left", padx=_sc(6, s),
                      ipadx=_sc(14, s), ipady=_sc(7, s))
            val = ctk.CTkLabel(
                card, text="0",
                font=(None, _sc(20, s), "bold"), text_color=color)
            val.pack()
            ctk.CTkLabel(
                card, text=label, font=(None, _sc(9, s)),
                text_color=C_MUTED).pack()
            setattr(self, attr_val, val)

        stat_card("FILES SCANNED",  "_v_scanned",  C_ACCENT)
        stat_card("CHANGED",        "_v_changed",  C_WARN)
        stat_card("CHANGE RATE",    "_v_rate",     C_TEXT)
        stat_card("FILES / SEC",    "_v_fps",      C_BLUE)
        stat_card("DIRS SKIPPED",   "_v_skip",     C_MUTED)

        # Current directory bar
        dir_frame = ctk.CTkFrame(self, fg_color=C_SURFACE, corner_radius=8)
        dir_frame.pack(fill="x", padx=_sc(24, s), pady=(_sc(4, s), 0))

        _label(dir_frame, "SCANNING:", size=_sc(9, s), weight="bold",
               color=C_MUTED).pack(
            side="left", padx=(_sc(10, s), 4), pady=_sc(8, s))
        self._dir_lbl = _label(dir_frame, "", size=_sc(10, s), color=C_TEXT)
        self._dir_lbl.pack(
            side="left", fill="x", expand=True,
            padx=(0, _sc(10, s)), pady=_sc(8, s))

        # Pause / Abort control row
        ctrl = ctk.CTkFrame(self, fg_color="transparent")
        ctrl.pack(pady=(_sc(10, s), 0))

        self._pause_btn = ctk.CTkButton(
            ctrl, text="  Pause  ",
            fg_color=C_CARD, border_color=C_BORDER, border_width=1,
            text_color=C_TEXT, hover_color=C_BORDER,
            font=(None, _sc(11, s)), width=_sc(110, s),
            height=_sc(30, s), command=self._on_pause)
        self._pause_btn.pack(side="left", padx=_sc(6, s))

        self._abort_btn = ctk.CTkButton(
            ctrl, text="  Abort Scan  ",
            fg_color=C_CARD, border_color=C_ERR, border_width=1,
            text_color=C_ERR, hover_color=_darken(C_ERR, 60),
            font=(None, _sc(11, s)), width=_sc(130, s),
            height=_sc(30, s), command=self._on_abort)
        self._abort_btn.pack(side="left", padx=_sc(6, s))

        # Callbacks set by _run_job_async
        self._pause_fn: Optional[callable] = None
        self._abort_fn: Optional[callable] = None
        self._is_paused = False

        self._tick()

    def _on_pause(self):
        if self._pause_fn is None:
            return
        self._is_paused = not self._is_paused
        self._pause_fn(self._is_paused)
        if self._is_paused:
            self._pause_btn.configure(
                text="  Resume  ",
                fg_color=C_WARN, text_color="#000000",
                border_color=C_WARN)
            self._pct_lbl.configure(text="Paused")
            self._dir_lbl.configure(
                text="Scan paused - press Resume to continue")
        else:
            self._pause_btn.configure(
                text="  Pause  ",
                fg_color=C_CARD, text_color=C_TEXT,
                border_color=C_BORDER)
            self._pct_lbl.configure(text="Resuming...")

    def _on_abort(self):
        if self._abort_fn is None:
            return
        self._abort_fn()
        self._abort_btn.configure(
            text="  Aborting...  ", state="disabled",
            fg_color=C_CARD, text_color=C_MUTED, border_color=C_BORDER)
        self._pause_btn.configure(state="disabled")
        self._pct_lbl.configure(text="Aborting scan...")
        self._dir_lbl.configure(
            text="Waiting for worker threads to stop")

    def bind_controls(self, pause_fn, abort_fn):
        """Wired by _run_job_async to connect scanner stop/pause to buttons."""
        self._pause_fn  = pause_fn
        self._abort_fn  = abort_fn
        self._is_paused = False

    def _reset_buttons(self):
        self._pause_btn.configure(
            text="  Pause  ", state="normal",
            fg_color=C_CARD, text_color=C_TEXT, border_color=C_BORDER)
        self._abort_btn.configure(
            text="  Abort Scan  ", state="normal",
            fg_color=C_CARD, text_color=C_ERR, border_color=C_ERR)
        self._pause_fn  = None
        self._abort_fn  = None
        self._is_paused = False

    def _tick(self):
        if self._start_time:
            elapsed = time.monotonic() - self._start_time
            m, sv = divmod(int(elapsed), 60)
            self._elapsed_lbl.configure(
                text="Elapsed: " + str(m) + "m " + str(sv) + "s")
        self.after(1000, self._tick)

    def start(self, job_name: str, engine: str = "parallel", workers: int = 0):
        self._start_time = time.monotonic()
        self._job_lbl.configure(text=job_name)
        self._reset_buttons()

        if workers > 0:
            self._engine_badge.configure(
                text="  " + str(workers) + " THREADS  ",
                fg_color=C_BLUE, text_color="#000000")
        else:
            self._engine_badge.configure(
                text="  PARALLEL  ", fg_color=C_BORDER,
                text_color=C_MUTED)

        for attr in ("_v_scanned", "_v_changed", "_v_rate",
                     "_v_fps", "_v_skip"):
            getattr(self, attr).configure(text="0")
        self._dir_lbl.configure(text="Initialising...")
        self._pct_lbl.configure(text="Scanning...")
        self._eta_lbl.configure(text="")
        self._bar.configure(mode="indeterminate")
        self._bar.start()

    def update(self, p: ScanProgress):
        scanned = p.scanned
        changed = p.changed
        total   = p.total_hint
        rate    = round(changed / scanned * 100, 1) if scanned > 0 else 0.0

        self._v_scanned.configure(
            text=("{:,}".format(scanned)) +
                 ("/" + "{:,}".format(total) if total > 0 else ""))
        self._v_changed.configure(text="{:,}".format(changed))
        self._v_rate.configure(
            text=str(rate) + "%",
            text_color=(C_ERR if rate > 50
                        else C_WARN if rate > 20 else C_OK))
        self._v_fps.configure(text="{:,.0f}".format(p.files_per_sec))
        self._v_skip.configure(text=str(p.skipped_dirs))

        if total > 10 and scanned <= total:
            pct_val = scanned / total
            try:
                self._bar.stop()
                self._bar.configure(mode="determinate")
                self._bar.set(pct_val)
            except Exception:
                pass
            self._pct_lbl.configure(
                text=str(int(pct_val * 100)) + "% complete")

            elapsed = time.monotonic() - self._start_time
            if pct_val > 0.02 and elapsed > 1:
                rem = (elapsed / pct_val) * (1.0 - pct_val)
                eta = (
                    str(int(rem)) + "s" if rem < 60
                    else str(int(rem / 60)) + "m " +
                         str(int(rem % 60)) + "s")
                self._eta_lbl.configure(text=eta + " remaining")

        disp = p.current_dir
        if len(disp) > 65:
            disp = "..." + disp[-62:]
        self._dir_lbl.configure(text=disp)

    def finish(self):
        try:
            self._bar.stop()
            self._bar.configure(mode="determinate")
            self._bar.set(1.0)
        except Exception:
            pass
        self._pct_lbl.configure(text="100% complete")
        self._eta_lbl.configure(text="Done")
        self._dir_lbl.configure(text="Scan complete")
        self._start_time = 0.0
        self._reset_buttons()


# ---------------------------------------------------------------------------
# Threshold override dialog
# ---------------------------------------------------------------------------

class ThresholdDialog(ctk.CTkToplevel):
    """
    Blocking modal shown on the main thread when a job exceeds its threshold.
    Worker thread waits on self.event; result stored in self.proceed (bool).
    """

    def __init__(self, parent, job_name: str, pct: float,
                 threshold: int, total: int, changed: int):
        super().__init__(parent)
        self.proceed = False
        self.event   = threading.Event()

        s = getattr(parent, "_scale", 1.0)

        self.title("Threshold Exceeded")
        self.configure(fg_color=C_BG)
        self.resizable(False, False)
        self.grab_set()
        self.lift()
        self.focus_force()
        self.protocol("WM_DELETE_WINDOW", self._abort)

        w, h = _sc(460, s), _sc(310, s)
        px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
        py = parent.winfo_y() + (parent.winfo_height() - h) // 2
        self.geometry(
            str(w) + "x" + str(h) + "+" + str(px) + "+" + str(py))

        # Warning icon row
        icon_row = ctk.CTkFrame(
            self, fg_color=C_ERR, corner_radius=0, height=_sc(6, s))
        icon_row.pack(fill="x")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True,
                  padx=_sc(28, s), pady=_sc(20, s))

        _label(body, "Threshold Exceeded",
               size=_sc(16, s), weight="bold", color=C_ERR).pack(
            anchor="w")

        _label(body, "Job:  " + job_name,
               size=_sc(11, s), color=C_MUTED).pack(
            anchor="w", pady=(_sc(10, s), 0))

        # Stats card row
        cards = ctk.CTkFrame(body, fg_color="transparent")
        cards.pack(fill="x", pady=_sc(14, s))

        def _card(label, value, color):
            f = ctk.CTkFrame(cards, fg_color=C_CARD, corner_radius=8)
            f.pack(side="left", expand=True, fill="x",
                   padx=_sc(4, s), ipadx=_sc(10, s), ipady=_sc(6, s))
            ctk.CTkLabel(
                f, text=value,
                font=(None, _sc(18, s), "bold"),
                text_color=color).pack()
            ctk.CTkLabel(
                f, text=label,
                font=(None, _sc(9, s)),
                text_color=C_MUTED).pack()

        _card("CHANGED",   "{:,}".format(changed),          C_WARN)
        _card("TOTAL",     "{:,}".format(total),            C_TEXT)
        _card("RATE",      str(pct) + "%",                  C_ERR)
        _card("THRESHOLD", str(threshold) + "%",            C_MUTED)

        _label(body,
               "This many files changed in the monitored window.\n"
               "Proceeding may sync a large unintended change.",
               size=_sc(10, s), color=C_MUTED, justify="left").pack(
            anchor="w", pady=(_sc(2, s), _sc(16, s)))

        # Buttons
        btn_row = ctk.CTkFrame(body, fg_color="transparent")
        btn_row.pack(fill="x")

        _btn(btn_row, "Abort (safe)",
             self._abort, color=C_CARD, text_color=C_TEXT,
             width=_sc(130, s), height=_sc(36, s)).pack(side="left")

        _btn(btn_row, "Proceed Anyway",
             self._proceed, color=C_ERR,
             width=_sc(150, s), height=_sc(36, s)).pack(side="right")

    def _abort(self):
        self.proceed = False
        self.event.set()
        self.destroy()

    def _proceed(self):
        self.proceed = True
        self.event.set()
        self.destroy()


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class SyncGuardApp(ctk.CTk):

    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self._sw, self._sh, self._scale = _screen_info()
        s = self._scale

        w = min(_sc(1100, s), int(self._sw * 0.90))
        h = min(_sc(750,  s), int(self._sh * 0.88))
        x = (self._sw - w) // 2
        y = (self._sh - h) // 2
        self.geometry(str(w) + "x" + str(h) + "+" + str(x) + "+" + str(y))
        self.minsize(_sc(780, s), _sc(500, s))
        self.title(APP_NAME + " " + __import__("syncguard").APP_VER)
        self.configure(fg_color=C_BG)

        self.store           = JobStore()
        self.selected_index: Optional[int] = None
        self.job_rows:       List[JobRow]  = []
        self._statuses:      dict          = {}
        self._sched_times:   List[str]     = []
        self._scanning:      set           = set()  # stable job IDs
        self._state_lock = threading.RLock()
        self._guardians:     dict          = {}

        # Thread-safe log queue
        self._log_queue: _queue.Queue = _queue.Queue()

        self._build_ui()
        self._drain_log_queue()
        self._append_log(
            APP_NAME + " " + __import__("syncguard").APP_VER +
            " started  [screen " + str(self._sw) + "x" + str(self._sh) +
            "  scale=" + str(round(s, 2)) + "]", "INFO")
        self._append_log(
            "Scan mode: full parallel rescan on every run", "INFO")
        for warning in list(_STARTUP_WARNINGS):
            self._append_log(warning, "WARN")

        self.scheduler = SchedulerService(
            self.store, self._append_log, self._run_job_async)
        self.scheduler.rebuild()
        self.scheduler.start()

        self._refresh_job_list()
        if self.store.jobs:
            self._select_job(0)
        else:
            self._show_no_selection()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -------------------------------------------------------------------
    # UI Build
    # -------------------------------------------------------------------

    def _build_ui(self):
        s = self._scale

        # Topbar
        topbar = ctk.CTkFrame(
            self, fg_color=C_SURFACE, corner_radius=0, height=_sc(50, s))
        topbar.pack(fill="x", side="top")
        topbar.pack_propagate(False)
        self._topbar_ref = topbar

        ctk.CTkLabel(
            topbar, text="  " + APP_NAME,
            font=(None, _sc(17, s), "bold"),
            text_color=C_ACCENT).pack(side="left", padx=4)
        if self._sw > 900:
            ctk.CTkLabel(
                topbar, text="FreeFileSync Job Manager",
                font=(None, _sc(10, s)),
                text_color=C_MUTED).pack(side="left", padx=4)

        _btn(topbar, "Run All", self._run_all, color=C_BLUE,
             width=_sc(85, s), height=_sc(30, s)).pack(
            side="right", padx=_sc(8, s), pady=_sc(10, s))
        _btn(topbar, "+ New Job", self._new_job,
             width=_sc(95, s), height=_sc(30, s)).pack(
            side="right", padx=4, pady=_sc(10, s))

        # Log panel (bottom)
        ctk.CTkFrame(
            self, fg_color=C_BORDER, height=1).pack(fill="x", side="bottom")

        log_h = (_sc(110, s) if self._sh <= 800 else
                 _sc(140, s) if self._sh <= 900 else _sc(170, s))
        log_panel = ctk.CTkFrame(
            self, fg_color=C_SURFACE, corner_radius=0, height=log_h)
        log_panel.pack(fill="x", side="bottom")
        log_panel.pack_propagate(False)

        log_hdr = ctk.CTkFrame(log_panel, fg_color="transparent")
        log_hdr.pack(fill="x", padx=_sc(12, s), pady=(_sc(5, s), 2))
        _label(log_hdr, "ACTIVITY LOG", size=_sc(9, s), weight="bold",
               color=C_MUTED).pack(side="left")
        _btn(log_hdr, "Clear", self._clear_log, color=C_BORDER,
             height=_sc(20, s), width=_sc(52, s),
             text_color=C_MUTED).pack(side="right")

        self.log_box = ctk.CTkTextbox(
            log_panel, fg_color=C_BG, text_color=C_TEXT,
            font=("Consolas", _sc(10, s)), corner_radius=0,
            border_width=0, wrap="word")
        self.log_box.pack(fill="both", expand=True,
                          padx=_sc(12, s), pady=(0, _sc(6, s)))
        self.log_box.configure(state="disabled")

        tb = self.log_box._textbox
        for tag, col in [("INFO", C_TEXT), ("OK", C_OK),
                         ("WARN", C_WARN), ("ERROR", C_ERR),
                         ("ABORTED", C_ERR)]:
            tb.tag_configure(tag, foreground=col)

        # Body
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)

        sidebar = ctk.CTkFrame(
            body, fg_color=C_SURFACE, corner_radius=0, width=_sc(195, s))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        _label(sidebar, "JOBS", size=_sc(9, s), weight="bold",
               color=C_MUTED).pack(
            anchor="w", padx=_sc(12, s), pady=(_sc(12, s), 4))
        self.job_list_frame = ctk.CTkScrollableFrame(
            sidebar, fg_color="transparent", corner_radius=0)
        self.job_list_frame.pack(fill="both", expand=True)

        sf = ctk.CTkFrame(sidebar, fg_color="transparent")
        sf.pack(fill="x", padx=_sc(7, s), pady=_sc(7, s))
        _btn(sf, "Remove Job", self._remove_job,
             color=C_ERR, height=_sc(28, s)).pack(fill="x")

        ctk.CTkFrame(
            body, fg_color=C_BORDER, width=1).pack(side="left", fill="y")

        self.main_panel = ctk.CTkFrame(body, fg_color="transparent")
        self.main_panel.pack(side="left", fill="both", expand=True)

        self.no_sel_frame = ctk.CTkFrame(
            self.main_panel, fg_color="transparent")
        _label(self.no_sel_frame,
               "Select a job or create a new one",
               size=_sc(14, s), color=C_MUTED).place(
            relx=0.5, rely=0.45, anchor="center")

        self.detail_frame = ctk.CTkFrame(
            self.main_panel, fg_color="transparent")
        self._build_detail_panel()

        self.scan_panel = ScanProgressPanel(self.main_panel, s)

    def _build_detail_panel(self):
        s = self._scale
        df = self.detail_frame

        hdr = ctk.CTkFrame(df, fg_color="transparent")
        hdr.pack(fill="x", padx=_sc(16, s), pady=(_sc(10, s), 0))

        self.job_title_lbl = _label(
            hdr, "Job Configuration", size=_sc(15, s), weight="bold")
        self.job_title_lbl.pack(side="left")

        self.status_badge = ctk.CTkLabel(
            hdr, text="  IDLE  ", font=(None, _sc(9, s), "bold"),
            fg_color=C_BORDER, corner_radius=4, text_color=C_MUTED)
        self.status_badge.pack(side="left", padx=_sc(8, s))

        ar = ctk.CTkFrame(hdr, fg_color="transparent")
        ar.pack(side="right")
        _btn(ar, "Save", self._save_job,
             width=_sc(70, s), height=_sc(28, s)).pack(side="right")
        _btn(ar, "Run Now", self._run_current, color=C_BLUE,
             width=_sc(84, s), height=_sc(28, s)).pack(
            side="right", padx=_sc(6, s))

        self.tabs = ctk.CTkTabview(
            df, fg_color=C_SURFACE,
            segmented_button_fg_color=C_CARD,
            segmented_button_selected_color=C_ACCENT,
            segmented_button_selected_hover_color=_darken(C_ACCENT),
            segmented_button_unselected_color=C_CARD,
            segmented_button_unselected_hover_color=C_BORDER,
            text_color=C_TEXT, text_color_disabled=C_MUTED,
            corner_radius=8)
        self.tabs.pack(
            fill="both", expand=True,
            padx=_sc(16, s), pady=_sc(8, s))
        self.tabs.add("Config")
        self.tabs.add("Schedule")
        self.tabs.add("History")
        self.tabs.add("Guardian")
        self.tabs.add("Ransomware")

        self._build_config_tab(self.tabs.tab("Config"))
        self._build_schedule_tab(self.tabs.tab("Schedule"))
        self._build_history_tab(self.tabs.tab("History"))
        self._build_guardian_tab(self.tabs.tab("Guardian"))
        self._build_ransomware_tab(self.tabs.tab("Ransomware"))

    def _build_config_tab(self, tab):
        s = self._scale
        tab.configure(fg_color="transparent")
        cf = ctk.CTkScrollableFrame(
            tab, fg_color="transparent", corner_radius=0)
        cf.pack(fill="both", expand=True, padx=2, pady=2)

        lbl_w = _sc(105, s)
        row_h = _sc(30, s)
        pad_y = _sc(3, s)

        def path_row(label, attr, is_dir=False):
            row = ctk.CTkFrame(cf, fg_color="transparent")
            row.pack(fill="x", pady=pad_y)
            ctk.CTkLabel(
                row, text=label, width=lbl_w, anchor="w",
                font=(None, _sc(11, s)), text_color=C_MUTED).pack(
                side="left")
            entry = _entry(row, height=row_h)
            entry.pack(side="left", fill="x", expand=True)
            setattr(self, attr, entry)

            def browse(a=attr, d=is_dir):
                p = (filedialog.askdirectory() if d
                     else filedialog.askopenfilename())
                if p:
                    e = getattr(self, a)
                    e.delete(0, "end")
                    e.insert(0, p)

            ctk.CTkButton(
                row, text="...", width=_sc(30, s), height=row_h,
                fg_color=C_CARD, border_color=C_BORDER, border_width=1,
                text_color=C_TEXT, hover_color=C_BORDER,
                command=browse).pack(side="left", padx=(3, 0))

        def text_row(label, attr):
            row = ctk.CTkFrame(cf, fg_color="transparent")
            row.pack(fill="x", pady=pad_y)
            ctk.CTkLabel(
                row, text=label, width=lbl_w, anchor="w",
                font=(None, _sc(11, s)), text_color=C_MUTED).pack(
                side="left")
            entry = _entry(row, height=row_h)
            entry.pack(side="left", fill="x", expand=True)
            setattr(self, attr, entry)

        def slider_row(label, attr_s, attr_l, lo, hi, unit, default, cmd):
            row = ctk.CTkFrame(cf, fg_color="transparent")
            row.pack(fill="x", pady=_sc(5, s))
            ctk.CTkLabel(
                row, text=label, width=lbl_w, anchor="w",
                font=(None, _sc(11, s)), text_color=C_MUTED).pack(
                side="left")
            val_lbl = ctk.CTkLabel(
                row, text=str(default) + unit, width=_sc(50, s),
                font=(None, _sc(11, s), "bold"), text_color=C_ACCENT)
            val_lbl.pack(side="right")
            slider = ctk.CTkSlider(
                row, from_=lo, to=hi,
                fg_color=C_CARD, progress_color=C_ACCENT,
                button_color=C_ACCENT,
                button_hover_color=_darken(C_ACCENT), command=cmd)
            slider.pack(side="left", fill="x", expand=True,
                        padx=_sc(6, s))
            slider.set(default)
            setattr(self, attr_s, slider)
            setattr(self, attr_l, val_lbl)

        text_row("Job Name",    "e_name")
        path_row("Source Path", "e_source",  is_dir=True)
        path_row("FFS Exe",     "e_ffsexe")
        path_row("Batch File",  "e_batch")
        path_row("Log File",    "e_logfile")

        self.path_status_lbl = _label(
            cf, "", size=_sc(9, s), color=C_MUTED)
        self.path_status_lbl.pack(
            anchor="w", padx=_sc(2, s), pady=(0, _sc(3, s)))

        slider_row("Threshold",  "thr_slider", "thr_lbl",
                   1,  99, "%",  40, self._on_threshold)
        slider_row("Hours Back", "hrs_slider", "hrs_lbl",
                   1, 168, "h",  24, self._on_hours)
        slider_row("Workers",    "wrk_slider", "wrk_lbl",
                   0,  64, "",    0, self._on_workers)

        self.wrk_hint = _label(
            cf, "Workers: 0 = Auto (8 local / 32 network)",
            size=_sc(9, s), color=C_MUTED)
        self.wrk_hint.pack(anchor="w", padx=_sc(2, s))

        # Exclude patterns
        ctk.CTkFrame(
            cf, fg_color=C_BORDER, height=1).pack(
            fill="x", pady=_sc(6, s))
        excl_hdr = ctk.CTkFrame(cf, fg_color="transparent")
        excl_hdr.pack(fill="x")
        _label(excl_hdr, "Exclude Patterns",
               size=_sc(11, s), weight="bold").pack(side="left")
        _label(excl_hdr,
               "  (*.tmp, ~$*, .git - one per line, case-insensitive glob)",
               size=_sc(9, s), color=C_MUTED).pack(side="left")
        self.e_exclude = ctk.CTkTextbox(
            cf, height=_sc(60, s), fg_color=C_CARD,
            border_color=C_BORDER, border_width=1,
            text_color=C_TEXT, font=("Consolas", _sc(10, s)))
        self.e_exclude.pack(fill="x", pady=(_sc(4, s), 0))

        sw_row = ctk.CTkFrame(cf, fg_color="transparent")
        sw_row.pack(fill="x", pady=pad_y)
        ctk.CTkLabel(
            sw_row, text="Enabled", width=lbl_w, anchor="w",
            font=(None, _sc(11, s)), text_color=C_MUTED).pack(side="left")
        self.enabled_sw = ctk.CTkSwitch(
            sw_row, text="", progress_color=C_ACCENT,
            button_color=C_TEXT, button_hover_color=C_MUTED)
        self.enabled_sw.pack(side="left")
        self.enabled_sw.select()

        # Cache + watcher info
        ctk.CTkFrame(
            cf, fg_color=C_BORDER, height=1).pack(
            fill="x", pady=_sc(7, s))

        cache_row = ctk.CTkFrame(cf, fg_color="transparent")
        cache_row.pack(fill="x", pady=pad_y)
        self.cache_info_lbl = _label(
            cache_row, "Cache: not loaded",
            size=_sc(10, s), color=C_MUTED)
        self.cache_info_lbl.pack(side="left")
        _btn(cache_row, "Clear Cache", self._clear_cache,
             color=C_BORDER, text_color=C_MUTED,
             height=_sc(24, s), width=_sc(88, s)).pack(side="right")

        self.watch_info_lbl = _label(
            cf, "Full rescan mode  (every job run)",
            size=_sc(10, s), color=C_MUTED)
        self.watch_info_lbl.pack(anchor="w", pady=(2, 0))

    def _build_schedule_tab(self, tab):
        s = self._scale
        tab.configure(fg_color="transparent")

        _label(tab, "Scheduled Run Times",
               size=_sc(12, s), weight="bold").pack(anchor="w", pady=(4, 2))
        _label(tab, "Runs every day at these times  (24h format)",
               size=_sc(10, s), color=C_MUTED).pack(
            anchor="w", pady=(0, _sc(8, s)))

        self.sched_list_frame = ctk.CTkScrollableFrame(
            tab, fg_color=C_CARD, corner_radius=8, height=_sc(130, s))
        self.sched_list_frame.pack(fill="x")

        add_row = ctk.CTkFrame(tab, fg_color="transparent")
        add_row.pack(fill="x", pady=_sc(8, s))
        self.new_time_entry = _entry(
            add_row, placeholder_text="HH:MM  e.g. 02:00",
            width=_sc(145, s), height=_sc(30, s))
        self.new_time_entry.pack(side="left")
        _btn(add_row, "+ Add", self._add_sched_time,
             height=_sc(30, s)).pack(side="left", padx=_sc(6, s))

        self.next_run_lbl = _label(
            tab, "", size=_sc(10, s), color=C_MUTED)
        self.next_run_lbl.pack(anchor="w", pady=4)

        self.countdown_lbl = ctk.CTkLabel(
            tab, text="",
            font=(None, _sc(12, s), "bold"), text_color=C_ACCENT)
        self.countdown_lbl.pack(anchor="w")
        self._tick_countdown()

    # -------------------------------------------------------------------
    # Job list helpers
    # -------------------------------------------------------------------

    def _refresh_job_list(self):
        for w in self.job_list_frame.winfo_children():
            w.destroy()
        self.job_rows.clear()
        for i, job in enumerate(self.store.jobs):
            row = JobRow(self.job_list_frame, job, self._select_job,
                         i, self._scale)
            row.set_ctx_callback(self._on_job_ctx)
            row.pack(fill="x", padx=_sc(4, self._scale), pady=2)
            row.set_status(self._statuses.get(job.name, "IDLE"))
            if i == self.selected_index:
                row.set_selected(True)
            self.job_rows.append(row)

    def _on_job_ctx(self, index: int, action: str):
        if action == "run":
            self._select_job(index)
            self._save_job()
            self._run_job_async(self.store.jobs[index], "manual")
        elif action == "dup":
            self._duplicate_job(index)
        elif action == "remove":
            self._select_job(index)
            self._remove_job()

    def _duplicate_job(self, index: int):
        src = self.store.jobs[index]
        dup = copy.deepcopy(src)
        dup.job_id = uuid.uuid4().hex
        dup.name = src.name + " (copy)"
        self.store.add(dup)
        new_idx = len(self.store.jobs) - 1
        self._refresh_job_list()
        self._select_job(new_idx)
        self._append_log("Duplicated job: " + dup.name, "OK")

    def _show_no_selection(self):
        self.scan_panel.place_forget()
        self.detail_frame.place_forget()
        self.no_sel_frame.place(relx=0, rely=0, relwidth=1, relheight=1)

    def _show_detail(self):
        self.scan_panel.place_forget()
        self.no_sel_frame.place_forget()
        self.detail_frame.place(relx=0, rely=0, relwidth=1, relheight=1)

    def _show_scan_progress(self):
        self.detail_frame.place_forget()
        self.no_sel_frame.place_forget()
        self.scan_panel.place(relx=0, rely=0, relwidth=1, relheight=1)

    def _select_job(self, index: int):
        self.selected_index = index
        for i, row in enumerate(self.job_rows):
            row.set_selected(i == index)
        job = self.store.jobs[index]
        self._load_job_into_form(job)
        if job.name in self._scanning:
            self._show_scan_progress()
        else:
            self._show_detail()
        if hasattr(self, "_deferred_load_id"):
            try:
                self.after_cancel(self._deferred_load_id)
            except Exception:
                pass
        self._deferred_load_id = self.after(
            80, lambda j=job: self._deferred_load(j))

    def _load_job_into_form(self, job: JobConfig):
        """Fast path: only update widgets from already-in-memory data."""
        self.job_title_lbl.configure(text=job.name)
        for entry, val in [
            (self.e_name,    job.name),
            (self.e_source,  job.source_path),
            (self.e_ffsexe,  job.ffs_exe),
            (self.e_batch,   job.batch_file),
            (self.e_logfile, job.log_file),
        ]:
            entry.delete(0, "end")
            entry.insert(0, val)
        self.thr_slider.set(job.threshold)
        self.hrs_slider.set(job.hours_back)
        self.wrk_slider.set(job.num_workers)
        self.thr_lbl.configure(text=str(job.threshold) + "%")
        self.hrs_lbl.configure(text=str(job.hours_back) + "h")
        self._update_workers_lbl(job.num_workers)
        if job.enabled:
            self.enabled_sw.select()
        else:
            self.enabled_sw.deselect()
        self.e_exclude.delete("1.0", "end")
        self.e_exclude.insert("1.0", "\n".join(job.exclude_patterns))
        self._sched_times = list(job.schedule_times)
        self._rebuild_sched_list()
        self._set_status_badge(self._statuses.get(job.name, "IDLE"))
        self.cache_info_lbl.configure(text="Cache: loading...")
        self.watch_info_lbl.configure(
            text="Full rescan mode  (every job run)")
        self._clear_history_display()
        self._load_guardian_into_tab(job)
        self._load_ransomware_into_tab(job)

    def _deferred_load(self, job: JobConfig):
        """
        Runs 80ms after job selection. Offloads disk I/O to a background
        thread so the main thread never blocks.
        """
        if (self.selected_index is None or
                self.store.jobs[self.selected_index].name != job.name):
            return

        def _bg():
            try:
                cache = (ScanCache(job.source_path)
                         if job.source_path else None)
                c_text = (
                    ("Cache: " + "{:,}".format(cache.known_total) +
                     " files  |  last scan " + cache.age_str)
                    if cache and cache.known_total > 0
                    else "Cache: empty  (first scan will build it)")
            except Exception:
                c_text = "Cache: unavailable"

            w_text = "Full rescan mode  (every job run)"

            try:
                records = ScanHistory(job.job_id).records
            except Exception:
                records = []

            def _apply():
                if (self.selected_index is None or
                        self.store.jobs[self.selected_index].name !=
                        job.name):
                    return
                self.cache_info_lbl.configure(text=c_text)
                self.watch_info_lbl.configure(text=w_text)
                self._render_history_batched(records)

            self.after(0, _apply)

        threading.Thread(
            target=_bg, daemon=True, name="sg-ui-load").start()

    def _collect_job_from_form(self) -> JobConfig:
        raw_excl = self.e_exclude.get("1.0", "end").strip()
        excl = [p.strip() for p in raw_excl.splitlines() if p.strip()]
        gdn_folder = (
            self.gdn_folder_entry.get().strip()
            if hasattr(self, "gdn_folder_entry") else "")
        gdn_auto_pause = (
            bool(self.gdn_auto_var.get())
            if self.gdn_auto_var else False)
        current_id = (
            self.store.jobs[self.selected_index].job_id
            if self.selected_index is not None
            else uuid.uuid4().hex)
        # Ransomware settings from tab
        rw = {}
        if hasattr(self, "rw_enabled_sw"):
            try:
                rw = self._collect_ransomware_from_tab()
            except Exception:
                pass
        return JobConfig(
            job_id             = current_id,
            name               = (self.e_name.get().strip()
                                  or "Unnamed Job"),
            source_path        = self.e_source.get().strip(),
            ffs_exe            = self.e_ffsexe.get().strip(),
            batch_file         = self.e_batch.get().strip(),
            log_file           = self.e_logfile.get().strip(),
            threshold          = int(self.thr_slider.get()),
            hours_back         = int(self.hrs_slider.get()),
            num_workers        = int(self.wrk_slider.get()),
            exclude_patterns   = excl,
            schedule_times     = list(self._sched_times),
            enabled            = bool(self.enabled_sw.get()),
            guardian_folder    = gdn_folder,
            guardian_auto_pause = gdn_auto_pause,
            # Ransomware protection
            destination_path       = rw.get("destination_path", ""),
            ransomware_protection  = rw.get("ransomware_protection", True),
            entropy_threshold      = rw.get("entropy_threshold", 7.5),
            snapshot_before_sync   = rw.get("snapshot_before_sync", True),
            max_snapshots          = 3,
            custom_extensions      = rw.get("custom_extensions", []),
            anomaly_block_score    = rw.get("anomaly_block_score", 60),
        )

    # -------------------------------------------------------------------
    # Cache + watcher info
    # -------------------------------------------------------------------

    def _update_cache_info(self, job: JobConfig):
        """Non-blocking: reads cache file in background thread."""
        self.cache_info_lbl.configure(text="Cache: loading...")

        def _bg():
            if not job.source_path:
                text = "Cache: no source path set"
            else:
                try:
                    cache = ScanCache(job.source_path)
                    n = cache.known_total
                    text = (
                        ("Cache: " + "{:,}".format(n) +
                         " files  |  last scan " + cache.age_str)
                        if n > 0
                        else "Cache: empty  (first scan will build it)")
                except Exception:
                    text = "Cache: unavailable"
            self.after(0, lambda t=text: (
                self.cache_info_lbl.configure(text=t)))

        threading.Thread(
            target=_bg, daemon=True, name="sg-cache").start()

    def _clear_cache(self):
        if self.selected_index is None:
            return
        job = self.store.jobs[self.selected_index]
        if not job.source_path:
            return
        ScanCache(job.source_path).clear()
        self._append_log("Cache cleared for: " + job.name, "WARN")
        self._update_cache_info(job)

    # -------------------------------------------------------------------
    # History tab
    # -------------------------------------------------------------------

    def _build_history_tab(self, tab):
        s = self._scale
        tab.configure(fg_color="transparent")

        hdr_row = ctk.CTkFrame(tab, fg_color="transparent")
        hdr_row.pack(fill="x", pady=(4, _sc(8, s)))
        _label(hdr_row, "Scan History",
               size=_sc(12, s), weight="bold").pack(side="left")
        _btn(hdr_row, "Export CSV", self._export_history_csv,
             color=C_BLUE, height=_sc(24, s),
             width=_sc(90, s)).pack(side="right", padx=_sc(4, s))
        _btn(hdr_row, "Clear History", self._clear_history,
             color=C_BORDER, text_color=C_MUTED,
             height=_sc(24, s), width=_sc(100, s)).pack(side="right")

        # Column headers
        col_hdr = ctk.CTkFrame(tab, fg_color=C_CARD, corner_radius=6)
        col_hdr.pack(fill="x", pady=(0, 2))
        for txt, wt, anchor in [
            ("Date / Time",  200, "w"),
            ("Status",        80, "center"),
            ("Total",         70, "e"),
            ("Changed",       70, "e"),
            ("Rate",          60, "e"),
            ("Duration",      70, "e"),
            ("Engine",        80, "center"),
            ("Triggered",     70, "center"),
        ]:
            ctk.CTkLabel(
                col_hdr, text=txt, width=_sc(wt, s),
                font=(None, _sc(9, s), "bold"),
                text_color=C_MUTED, anchor=anchor).pack(
                side="left", padx=_sc(6, s), pady=_sc(5, s))

        self.history_frame = ctk.CTkScrollableFrame(
            tab, fg_color="transparent", corner_radius=0)
        self.history_frame.pack(fill="both", expand=True)

        self.history_empty_lbl = _label(
            self.history_frame,
            "No scan history yet - run a job to start recording",
            size=_sc(11, s), color=C_MUTED)
        self.history_empty_lbl.pack(pady=_sc(20, s))

    # -------------------------------------------------------------------
    # Guardian tab
    # -------------------------------------------------------------------

    def _build_guardian_tab(self, tab):
        s = self._scale
        tab.configure(fg_color="transparent")

        _label(tab, "Folder Guardian",
               size=_sc(12, s), weight="bold").pack(
            anchor="w", pady=(4, 2))
        _label(tab,
               ("Blocks renames and logs deletions in real-time. "
                "Pause before running FreeFileSync."),
               size=_sc(9, s), color=C_MUTED).pack(
            anchor="w", pady=(0, _sc(8, s)))

        folder_row = ctk.CTkFrame(tab, fg_color="transparent")
        folder_row.pack(fill="x", pady=(_sc(2, s), _sc(4, s)))
        _label(folder_row, "Watch Folder", size=_sc(10, s),
               color=C_MUTED).pack(
            side="left", padx=(0, _sc(8, s)))
        self.gdn_folder_entry = _entry(
            folder_row, height=_sc(28, s))
        self.gdn_folder_entry.pack(
            side="left", fill="x", expand=True)

        def _browse_gdn():
            from tkinter import filedialog as _fd
            p = _fd.askdirectory()
            if p:
                self.gdn_folder_entry.delete(0, "end")
                self.gdn_folder_entry.insert(0, p)

        ctk.CTkButton(
            folder_row, text="...", width=_sc(30, s), height=_sc(28, s),
            fg_color=C_CARD, border_color=C_BORDER, border_width=1,
            text_color=C_TEXT, hover_color=C_BORDER,
            command=_browse_gdn).pack(side="left", padx=(3, 0))

        def _use_source():
            src = (self.e_source.get().strip()
                   if hasattr(self, "e_source") else "")
            if src:
                self.gdn_folder_entry.delete(0, "end")
                self.gdn_folder_entry.insert(0, src)

        _btn(folder_row, "\u2190 Source", _use_source,
             color=C_BORDER, text_color=C_MUTED,
             height=_sc(28, s), width=_sc(80, s)).pack(
            side="left", padx=(_sc(4, s), 0))

        self.gdn_status_lbl = _label(
            tab, "\u26ab  Not monitoring",
            size=_sc(10, s), color=C_MUTED)
        self.gdn_status_lbl.pack(
            anchor="w", pady=(_sc(4, s), _sc(8, s)))

        ctrl = ctk.CTkFrame(tab, fg_color="transparent")
        ctrl.pack(fill="x", pady=(0, _sc(8, s)))

        self.gdn_start_btn = _btn(
            ctrl, "\u25b6  Start", self._guardian_start,
            color=C_OK, text_color="#000000",
            width=_sc(100, s), height=_sc(32, s))
        self.gdn_start_btn.pack(
            side="left", padx=(0, _sc(6, s)))

        self.gdn_stop_btn = ctk.CTkButton(
            ctrl, text="\u23f9  Stop", command=self._guardian_stop,
            fg_color=C_CARD, border_color=C_ERR, border_width=1,
            text_color=C_ERR, hover_color=_darken(C_ERR, 60),
            width=_sc(100, s), height=_sc(32, s), state="disabled")
        self.gdn_stop_btn.pack(
            side="left", padx=(0, _sc(6, s)))

        self.gdn_pause_btn = ctk.CTkButton(
            ctrl, text="\u23f8  Pause",
            command=self._guardian_toggle_pause,
            fg_color=C_CARD, border_color=C_WARN, border_width=1,
            text_color=C_WARN, hover_color=_darken(C_WARN, 60),
            width=_sc(110, s), height=_sc(32, s), state="disabled")
        self.gdn_pause_btn.pack(side="left")

        auto_row = ctk.CTkFrame(tab, fg_color="transparent")
        auto_row.pack(fill="x", pady=(_sc(2, s), _sc(8, s)))
        if _PSUTIL_OK:
            self.gdn_auto_var = tk.BooleanVar(value=False)
            ctk.CTkCheckBox(
                auto_row,
                text="Auto-pause when FreeFileSync.exe is running",
                variable=self.gdn_auto_var,
                font=(None, _sc(10, s)),
                text_color=C_TEXT,
                command=self._guardian_auto_toggle,
            ).pack(side="left")
        else:
            self.gdn_auto_var = None
            _label(auto_row,
                   "Install psutil for auto-pause:  pip install psutil",
                   size=_sc(9, s), color=C_WARN).pack(side="left")

        ctk.CTkFrame(
            tab, fg_color=C_BORDER, height=1).pack(
            fill="x", pady=(_sc(4, s), _sc(8, s)))
        _label(tab,
               ("\U0001f512  .guardian_lockfile is restored if deleted.  "
                "All other deletions are logged only.  "
                "Restore via FreeFileSync."),
               size=_sc(9, s), color=C_MUTED, justify="left").pack(
            anchor="w")

    # -------------------------------------------------------------------
    # Ransomware protection tab
    # -------------------------------------------------------------------

    def _build_ransomware_tab(self, tab):
        s = self._scale
        tab.configure(fg_color="transparent")

        scroll = ctk.CTkScrollableFrame(
            tab, fg_color="transparent", corner_radius=0)
        scroll.pack(fill="both", expand=True, padx=2, pady=2)

        lbl_w = _sc(140, s)
        row_h = _sc(28, s)
        pad_y = _sc(3, s)

        _label(scroll, "Ransomware Protection",
               size=_sc(12, s), weight="bold").pack(
            anchor="w", pady=(4, 2))
        _label(scroll,
               ("Entropy + extension + anomaly scoring to detect "
                "and block ransomware before sync."),
               size=_sc(9, s), color=C_MUTED).pack(
            anchor="w", pady=(0, _sc(8, s)))

        # Enable toggle
        sw_row = ctk.CTkFrame(scroll, fg_color="transparent")
        sw_row.pack(fill="x", pady=pad_y)
        ctk.CTkLabel(sw_row, text="Enable Protection",
                     width=lbl_w, anchor="w",
                     font=(None, _sc(11, s)),
                     text_color=C_MUTED).pack(side="left")
        self.rw_enabled_sw = ctk.CTkSwitch(
            sw_row, text="", progress_color=C_ACCENT,
            button_color=C_TEXT, button_hover_color=C_MUTED)
        self.rw_enabled_sw.pack(side="left")
        self.rw_enabled_sw.select()

        # Destination path
        dest_row = ctk.CTkFrame(scroll, fg_color="transparent")
        dest_row.pack(fill="x", pady=pad_y)
        ctk.CTkLabel(dest_row, text="Destination Path",
                     width=lbl_w, anchor="w",
                     font=(None, _sc(11, s)),
                     text_color=C_MUTED).pack(side="left")
        self.rw_dest_entry = _entry(dest_row, height=row_h)
        self.rw_dest_entry.pack(side="left", fill="x", expand=True)

        def _browse_dest():
            p = filedialog.askdirectory()
            if p:
                self.rw_dest_entry.delete(0, "end")
                self.rw_dest_entry.insert(0, p)

        ctk.CTkButton(
            dest_row, text="...", width=_sc(30, s), height=row_h,
            fg_color=C_CARD, border_color=C_BORDER, border_width=1,
            text_color=C_TEXT, hover_color=C_BORDER,
            command=_browse_dest).pack(side="left", padx=(3, 0))

        # Snapshot toggle
        snap_row = ctk.CTkFrame(scroll, fg_color="transparent")
        snap_row.pack(fill="x", pady=pad_y)
        ctk.CTkLabel(snap_row, text="Pre-Sync Snapshot",
                     width=lbl_w, anchor="w",
                     font=(None, _sc(11, s)),
                     text_color=C_MUTED).pack(side="left")
        self.rw_snapshot_sw = ctk.CTkSwitch(
            snap_row, text="", progress_color=C_ACCENT,
            button_color=C_TEXT, button_hover_color=C_MUTED)
        self.rw_snapshot_sw.pack(side="left")
        self.rw_snapshot_sw.select()

        _label(snap_row,
               "  Capture destination manifest before each sync",
               size=_sc(9, s), color=C_MUTED).pack(side="left")

        # Entropy threshold slider
        ent_row = ctk.CTkFrame(scroll, fg_color="transparent")
        ent_row.pack(fill="x", pady=_sc(5, s))
        ctk.CTkLabel(ent_row, text="Entropy Threshold",
                     width=lbl_w, anchor="w",
                     font=(None, _sc(11, s)),
                     text_color=C_MUTED).pack(side="left")
        self.rw_entropy_lbl = ctk.CTkLabel(
            ent_row, text="7.5", width=_sc(50, s),
            font=(None, _sc(11, s), "bold"), text_color=C_ACCENT)
        self.rw_entropy_lbl.pack(side="right")
        self.rw_entropy_slider = ctk.CTkSlider(
            ent_row, from_=6.0, to=8.0,
            fg_color=C_CARD, progress_color=C_ACCENT,
            button_color=C_ACCENT,
            button_hover_color=_darken(C_ACCENT),
            command=self._on_entropy)
        self.rw_entropy_slider.pack(
            side="left", fill="x", expand=True, padx=_sc(6, s))
        self.rw_entropy_slider.set(7.5)

        _label(ent_row, "",
               size=_sc(9, s), color=C_MUTED).pack(side="left")

        # Anomaly block score slider
        score_row = ctk.CTkFrame(scroll, fg_color="transparent")
        score_row.pack(fill="x", pady=_sc(5, s))
        ctk.CTkLabel(score_row, text="Block Score",
                     width=lbl_w, anchor="w",
                     font=(None, _sc(11, s)),
                     text_color=C_MUTED).pack(side="left")
        self.rw_score_lbl = ctk.CTkLabel(
            score_row, text="60", width=_sc(50, s),
            font=(None, _sc(11, s), "bold"), text_color=C_ACCENT)
        self.rw_score_lbl.pack(side="right")
        self.rw_score_slider = ctk.CTkSlider(
            score_row, from_=30, to=90,
            fg_color=C_CARD, progress_color=C_ACCENT,
            button_color=C_ACCENT,
            button_hover_color=_darken(C_ACCENT),
            command=self._on_block_score)
        self.rw_score_slider.pack(
            side="left", fill="x", expand=True, padx=_sc(6, s))
        self.rw_score_slider.set(60)

        # Custom extensions
        ctk.CTkFrame(scroll, fg_color=C_BORDER, height=1).pack(
            fill="x", pady=_sc(6, s))
        _label(scroll, "Custom Suspicious Extensions",
               size=_sc(11, s), weight="bold").pack(
            anchor="w")
        _label(scroll,
               "  (one per line, e.g. .mylock - added to built-in list)",
               size=_sc(9, s), color=C_MUTED).pack(
            anchor="w", pady=(0, _sc(3, s)))
        self.rw_extensions = ctk.CTkTextbox(
            scroll, height=_sc(50, s), fg_color=C_CARD,
            border_color=C_BORDER, border_width=1,
            text_color=C_TEXT, font=("Consolas", _sc(10, s)))
        self.rw_extensions.pack(fill="x", pady=(_sc(4, s), 0))

        # Info box
        ctk.CTkFrame(scroll, fg_color=C_BORDER, height=1).pack(
            fill="x", pady=_sc(6, s))
        _label(scroll,
               ("Shield: blocks sync if anomaly score > threshold.\n"
                "  - Entropy: detects encrypted file content\n"
                "  - Extensions: detects ransomware naming patterns\n"
                "  - Deletion ratio: detects mass file deletion\n"
                "  - Rename ratio: detects mass file renaming\n\n"
                "  Score weights: change=40%, entropy=25pts, "
                "ext=20pts, del=15pts, rename=10pts"),
               size=_sc(9, s), color=C_MUTED, justify="left").pack(
            anchor="w", pady=(0, _sc(8, s)))

        # Snapshots info
        self.rw_snap_info = _label(
            scroll, "Snapshots: none yet",
            size=_sc(10, s), color=C_MUTED)
        self.rw_snap_info.pack(anchor="w")

    def _on_entropy(self, val):
        self.rw_entropy_lbl.configure(text=str(round(val, 1)))

    def _on_block_score(self, val):
        self.rw_score_lbl.configure(text=str(int(val)))

    def _load_ransomware_into_tab(self, job):
        """Populate ransomware tab fields when a job is selected."""
        try:
            if job.ransomware_protection:
                self.rw_enabled_sw.select()
            else:
                self.rw_enabled_sw.deselect()
            self.rw_dest_entry.delete(0, "end")
            self.rw_dest_entry.insert(0, job.destination_path)
            if job.snapshot_before_sync:
                self.rw_snapshot_sw.select()
            else:
                self.rw_snapshot_sw.deselect()
            self.rw_entropy_slider.set(job.entropy_threshold)
            self.rw_entropy_lbl.configure(
                text=str(job.entropy_threshold))
            self.rw_score_slider.set(job.anomaly_block_score)
            self.rw_score_lbl.configure(
                text=str(int(job.anomaly_block_score)))
            self.rw_extensions.delete("1.0", "end")
            self.rw_extensions.insert(
                "1.0", "\n".join(job.custom_extensions))
        except Exception:
            pass

    def _collect_ransomware_from_tab(self):
        """Read ransomware settings from the tab widgets."""
        raw = self.rw_extensions.get("1.0", "end").strip()
        exts = [p.strip() for p in raw.splitlines() if p.strip()]
        return {
            "ransomware_protection": bool(self.rw_enabled_sw.get()),
            "destination_path": self.rw_dest_entry.get().strip(),
            "snapshot_before_sync": bool(self.rw_snapshot_sw.get()),
            "entropy_threshold": round(self.rw_entropy_slider.get(), 1),
            "anomaly_block_score": int(self.rw_score_slider.get()),
            "custom_extensions": exts,
        }

    # -------------------------------------------------------------------
    # Guardian actions
    # -------------------------------------------------------------------

    def _guardian_log(self, msg: str, level: str = "INFO"):
        self._append_log("[Guardian] " + msg, level)

    def _guardian_state(self):
        if self.selected_index is None:
            return None
        job = self.store.jobs[self.selected_index]
        if job.name not in self._guardians:
            self._guardians[job.name] = _GuardianState()
        return self._guardians[job.name]

    def _guardian_is_paused(self, state):
        return state.paused or state.auto_paused

    def _guardian_start(self):
        state = self._guardian_state()
        if state is None:
            return
        if state.is_monitoring:
            self._guardian_log("Already monitoring.", "WARN")
            return

        folder = self.gdn_folder_entry.get().strip()
        if not folder:
            messagebox.showerror(
                "Guardian", "Enter a folder to watch.", parent=self)
            return
        if not os.path.isdir(folder):
            messagebox.showerror(
                "Guardian", "Folder not found:\n" + folder, parent=self)
            return

        lockfile = os.path.join(folder, ".guardian_lockfile")
        if not os.path.exists(lockfile):
            try:
                with open(lockfile, "w") as f:
                    f.write("Guardian lockfile \u2013 created " +
                            datetime.now().isoformat() + "\n")
                self._guardian_log("Created lockfile: " + lockfile)
            except Exception as exc:
                self._guardian_log(
                    "Could not create lockfile: " + str(exc), "WARN")

        state.folder       = folder
        state.paused       = False
        state.auto_paused  = False
        state.cooldown     = _GuardianCooldown(cooldown_ms=500)
        state.rename_reverter = _RenameReverter(
            self._guardian_log, state.cooldown)
        state.deletion_guard = _DeletionGuard(
            self._guardian_log, state.cooldown)

        handler = _GuardianEventHandler(
            rename_q      = state.rename_reverter.queue,
            delete_q      = state.deletion_guard.queue,
            cooldown      = state.cooldown,
            paused_getter=lambda st=state: self._guardian_is_paused(st),
        )
        obs = _WdObserver()
        obs.schedule(handler, folder, recursive=True)
        obs.start()
        state.observer      = obs
        state.is_monitoring = True

        self._guardian_log("Started watching: " + folder)
        self._guardian_log("Renames blocked. Deletions logged only.")
        self._update_guardian_ui(state)

        if self.gdn_auto_var and self.gdn_auto_var.get():
            self._guardian_auto_start(state)

    def _guardian_stop(self):
        state = self._guardian_state()
        if state is None or not state.is_monitoring:
            return

        if state.auto_job_id:
            try:
                self.after_cancel(state.auto_job_id)
            except Exception:
                pass
            state.auto_job_id = None

        try:
            if state.observer:
                state.observer.stop()
                state.observer.join(timeout=2.0)
        except Exception:
            pass
        if state.rename_reverter:
            state.rename_reverter.stop()
        if state.deletion_guard:
            state.deletion_guard.stop()

        state.observer        = None
        state.rename_reverter = None
        state.deletion_guard  = None
        state.cooldown        = None
        state.is_monitoring   = False
        state.paused          = False
        state.auto_paused     = False

        self._guardian_log("Stopped.")
        self._update_guardian_ui(state)

    def _guardian_toggle_pause(self):
        state = self._guardian_state()
        if state is None or not state.is_monitoring:
            return
        state.paused = not state.paused
        if state.paused:
            self._guardian_log("Protection paused (manual).", "WARN")
        else:
            self._guardian_log("Protection resumed.", "OK")
        self._update_guardian_ui(state)

    def _guardian_auto_toggle(self):
        state = self._guardian_state()
        if state is None:
            return
        if self.gdn_auto_var and self.gdn_auto_var.get():
            if state.is_monitoring:
                self._guardian_auto_start(state)
        else:
            if state.auto_job_id:
                try:
                    self.after_cancel(state.auto_job_id)
                except Exception:
                    pass
                state.auto_job_id = None
            if state.auto_paused:
                state.auto_paused = False
                self._guardian_log(
                    "Auto-detect disabled \u2013 protection resumed.", "OK")
                self._update_guardian_ui(state)

    def _guardian_auto_start(self, state):
        """Poll every 2 s for FreeFileSync.exe and auto-pause/resume."""
        if not _PSUTIL_OK:
            return

        def _check():
            if not state.is_monitoring:
                return
            found = False
            try:
                for proc in _psutil.process_iter(["name"]):
                    if ((proc.info["name"] or "").lower() ==
                            "freefilesync.exe"):
                        found = True
                        break
            except Exception:
                pass
            if found and not state.auto_paused:
                state.auto_paused = True
                self._guardian_log(
                    "FreeFileSync detected \u2013 auto-pausing.", "WARN")
                self._update_guardian_ui(state)
            elif not found and state.auto_paused:
                state.auto_paused = False
                self._guardian_log(
                    "FreeFileSync closed \u2013 protection resumed.", "OK")
                self._update_guardian_ui(state)
            state.auto_job_id = self.after(2000, _check)

        _check()

    def _update_guardian_ui(self, state):
        """Refresh guardian tab widgets to match current state."""
        try:
            if not state.is_monitoring:
                self.gdn_status_lbl.configure(
                    text="\u26ab  Not monitoring", text_color=C_MUTED)
                self.gdn_start_btn.configure(state="normal")
                self.gdn_stop_btn.configure(state="disabled")
                self.gdn_pause_btn.configure(
                    state="disabled", text="\u23f8  Pause",
                    fg_color=C_CARD, border_color=C_WARN,
                    text_color=C_WARN)
                return
            if state.paused:
                self.gdn_status_lbl.configure(
                    text="\u23f8  Protection paused (manual)",
                    text_color=C_WARN)
                self.gdn_pause_btn.configure(
                    text="\u25b6  Resume",
                    fg_color=C_OK, border_color=C_OK,
                    text_color="#000000")
            elif state.auto_paused:
                self.gdn_status_lbl.configure(
                    text="\u23f8  Auto-paused (FreeFileSync running)",
                    text_color=C_WARN)
                self.gdn_pause_btn.configure(
                    text="\u23f8  Pause",
                    fg_color=C_CARD, border_color=C_WARN,
                    text_color=C_WARN)
            else:
                self.gdn_status_lbl.configure(
                    text=("\U0001f7e2  Active \u2013 watching: " +
                          state.folder),
                    text_color=C_OK)
                self.gdn_pause_btn.configure(
                    text="\u23f8  Pause",
                    fg_color=C_CARD, border_color=C_WARN,
                    text_color=C_WARN)
            self.gdn_start_btn.configure(state="disabled")
            self.gdn_stop_btn.configure(state="normal")
            self.gdn_pause_btn.configure(state="normal")
        except Exception:
            pass

    def _load_guardian_into_tab(self, job):
        """Populate guardian tab fields and status when a job is selected."""
        try:
            state = self._guardians.get(job.name)
            folder = (state.folder if state and state.folder
                      else job.guardian_folder or job.source_path)
            self.gdn_folder_entry.delete(0, "end")
            self.gdn_folder_entry.insert(0, folder)

            if self.gdn_auto_var is not None:
                if job.guardian_auto_pause:
                    self.gdn_auto_var.set(True)
                else:
                    self.gdn_auto_var.set(False)

            if state:
                self._update_guardian_ui(state)
            else:
                self.gdn_status_lbl.configure(
                    text="\u26ab  Not monitoring", text_color=C_MUTED)
                self.gdn_start_btn.configure(state="normal")
                self.gdn_stop_btn.configure(state="disabled")
                self.gdn_pause_btn.configure(state="disabled")
        except Exception:
            pass

    def _stop_all_guardians(self):
        """Stop every active guardian on exit."""
        for state in list(self._guardians.values()):
            if not state.is_monitoring:
                continue
            try:
                if state.auto_job_id:
                    self.after_cancel(state.auto_job_id)
                if state.observer:
                    state.observer.stop()
                    state.observer.join(timeout=1.0)
                if state.rename_reverter:
                    state.rename_reverter.stop()
                if state.deletion_guard:
                    state.deletion_guard.stop()
            except Exception:
                pass

    # -------------------------------------------------------------------
    # History display
    # -------------------------------------------------------------------

    def _clear_history_display(self):
        for w in self.history_frame.winfo_children():
            w.destroy()
        _label(self.history_frame, "Loading...",
               size=_sc(11, self._scale),
               color=C_MUTED).pack(pady=_sc(14, self._scale))

    def _render_history_batched(self, records: list, _offset: int = 0,
                                _batch: int = 20):
        """
        Renders history rows in batches of _batch using after() so the main
        thread is never busy for more than ~5ms at a time.
        """
        if self.selected_index is None:
            return

        s = self._scale

        if _offset == 0:
            for w in self.history_frame.winfo_children():
                w.destroy()
            if not records:
                _label(self.history_frame,
                       "No scan history yet - run a job to start recording",
                       size=_sc(11, s), color=C_MUTED).pack(
                    pady=_sc(20, s))
                return

        status_colors = {
            "OK": C_OK, "WARN": C_WARN,
            "ERROR": C_ERR, "ABORTED": C_ERR,
            "RUNNING": C_BLUE, "IDLE": C_MUTED,
        }

        chunk = records[_offset: _offset + _batch]
        for i, rec in enumerate(chunk):
            idx    = _offset + i
            row_bg = C_CARD if idx % 2 == 0 else C_SURFACE
            row    = ctk.CTkFrame(self.history_frame,
                                  fg_color=row_bg, corner_radius=4)
            row.pack(fill="x", pady=1)

            ctk.CTkLabel(
                row, text=rec.get("ts", ""),
                width=_sc(200, s), anchor="w",
                font=("Consolas", _sc(10, s)),
                text_color=C_TEXT).pack(
                side="left", padx=_sc(6, s), pady=_sc(4, s))

            st  = rec.get("status", "?")
            stc = status_colors.get(st, C_MUTED)
            ctk.CTkLabel(
                row, text=" " + st + " ",
                width=_sc(80, s), anchor="center",
                font=(None, _sc(9, s), "bold"),
                fg_color=stc,
                text_color=("#000000" if st in ("OK", "WARN")
                            else C_TEXT),
                corner_radius=4).pack(
                side="left", padx=_sc(4, s), pady=_sc(4, s))

            total   = rec.get("total",   0)
            changed = rec.get("changed", 0)
            pct     = rec.get("pct",     0.0)
            dur     = rec.get("duration_s", 0.0)
            pct_col = (C_ERR if pct > 50
                       else C_WARN if pct > 20 else C_OK)

            for val, wid, col in [
                ("{:,}".format(total),   70, C_TEXT),
                ("{:,}".format(changed), 70,
                 C_WARN if changed > 0 else C_MUTED),
                (str(pct) + "%",         60, pct_col),
                (_fmt_dur(dur),          70, C_MUTED),
            ]:
                ctk.CTkLabel(
                    row, text=val, width=_sc(wid, s), anchor="e",
                    font=("Consolas", _sc(10, s)),
                    text_color=col).pack(
                    side="left", padx=_sc(4, s), pady=_sc(4, s))

            for val, wid in [
                (rec.get("engine", ""),        80),
                (rec.get("triggered_by", ""),  70),
            ]:
                ctk.CTkLabel(
                    row, text=val, width=_sc(wid, s), anchor="center",
                    font=(None, _sc(9, s)),
                    text_color=C_MUTED).pack(
                    side="left", padx=_sc(4, s), pady=_sc(4, s))

        next_offset = _offset + _batch
        if next_offset < len(records):
            self.after(10, lambda: self._render_history_batched(
                records, next_offset, _batch))

    def _refresh_history_tab(self, job_name: str):
        self._clear_history_display()

        def _bg():
            try:
                records = ScanHistory(next(
                    (j.job_id for j in self.store.jobs
                     if j.name == job_name), job_name)).records
            except Exception:
                records = []
            self.after(0, lambda r=records:
                       self._render_history_batched(r))

        threading.Thread(
            target=_bg, daemon=True, name="sg-hist").start()

    def _clear_history(self):
        if self.selected_index is None:
            return
        job = self.store.jobs[self.selected_index]
        if not messagebox.askyesno(
            "Clear History",
            "Clear all scan history for '" + job.name + "'?",
            parent=self):
            return
        ScanHistory(job.job_id).clear()
        self._refresh_history_tab(job.name)
        self._append_log("History cleared for: " + job.name, "WARN")

    def _export_history_csv(self):
        if self.selected_index is None:
            return
        job     = self.store.jobs[self.selected_index]
        records = ScanHistory(job.job_id).records
        if not records:
            messagebox.showinfo(
                "No History", "No history to export.", parent=self)
            return
        dest = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=job.name.replace(" ", "_") + "_history.csv",
            parent=self)
        if not dest:
            return
        cols = ["ts", "status", "total", "changed", "pct",
                "duration_s", "engine", "triggered_by"]
        try:
            with open(dest, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=cols,
                                   extrasaction="ignore")
                w.writeheader()
                w.writerows(records)
            self._append_log("History exported to: " + dest, "OK")
        except Exception as exc:
            messagebox.showerror("Export Failed", str(exc), parent=self)

    # -------------------------------------------------------------------
    # Schedule tab actions
    # -------------------------------------------------------------------

    def _tick_countdown(self):
        """Update countdown every 5s."""
        try:
            if not self._sched_times:
                self.countdown_lbl.configure(text="")
            else:
                now = datetime.now()
                candidates = []
                for t in self._sched_times:
                    try:
                        hh, mm = map(int, t.split(":"))
                        c = now.replace(
                            hour=hh, minute=mm, second=0, microsecond=0)
                        if c <= now:
                            c += timedelta(days=1)
                        candidates.append(c)
                    except ValueError:
                        pass
                if candidates:
                    nxt  = min(candidates)
                    diff = int((nxt - now).total_seconds())
                    h, rem = divmod(diff, 3600)
                    m, s   = divmod(rem, 60)
                    self.countdown_lbl.configure(
                        text=("Next run in  " + str(h) + "h " +
                              str(m) + "m " + str(s) + "s"))
        except Exception:
            pass
        self.after(5000, self._tick_countdown)

    def _validate_paths(self) -> str:
        """Synchronous check - called only on Save, never on keystrokes."""
        issues = []
        src = self.e_source.get().strip()
        if src and not os.path.exists(src):
            issues.append("Source path not found")
        exe = self.e_ffsexe.get().strip()
        if exe and not os.path.isfile(exe):
            issues.append("FFS exe not found")
        bat = self.e_batch.get().strip()
        if bat and not os.path.isfile(bat):
            issues.append("Batch file not found")
        return "  |  ".join(issues)

    def _on_path_field_change(self, *_):
        """Debounced: waits 600ms after last keystroke before checking."""
        if hasattr(self, "_path_check_id"):
            try:
                self.after_cancel(self._path_check_id)
            except Exception:
                pass
        self._path_check_id = self.after(600, self._do_validate_paths)

    def _do_validate_paths(self):
        """Run path validation in a background thread."""
        src = self.e_source.get().strip()
        exe = self.e_ffsexe.get().strip()
        bat = self.e_batch.get().strip()

        def _check():
            issues = []
            if src and not os.path.exists(src):
                issues.append("Source not found")
            if exe and not os.path.isfile(exe):
                issues.append("FFS exe not found")
            if bat and not os.path.isfile(bat):
                issues.append("Batch not found")
            result = "  |  ".join(issues)

            def _apply(r=result):
                if r:
                    self.path_status_lbl.configure(
                        text="Path issues: " + r, text_color=C_WARN)
                else:
                    self.path_status_lbl.configure(
                        text="All paths OK", text_color=C_OK)

            self.after(0, _apply)

        threading.Thread(
            target=_check, daemon=True, name="sg-pathchk").start()

    def _rebuild_sched_list(self):
        for w in self.sched_list_frame.winfo_children():
            w.destroy()
        if not self._sched_times:
            _label(self.sched_list_frame,
                   ("No scheduled times - use 'Run Now' "
                    "to run manually"),
                   color=C_MUTED,
                   size=_sc(10, self._scale)).pack(pady=12)
            self.next_run_lbl.configure(text="")
            return

        for t in self._sched_times:
            row = ctk.CTkFrame(
                self.sched_list_frame, fg_color="transparent")
            row.pack(fill="x", pady=3, padx=8)
            ctk.CTkLabel(
                row, text="  " + t,
                font=("Consolas", _sc(13, self._scale)),
                text_color=C_TEXT, anchor="w").pack(
                side="left", fill="x", expand=True)

            def _rm(time=t):
                self._sched_times.remove(time)
                self._rebuild_sched_list()

            _btn(row, "Remove", _rm, color=C_ERR,
                 height=_sc(25, self._scale),
                 width=_sc(72, self._scale)).pack(side="right")

        now = datetime.now()
        next_runs = []
        for t in self._sched_times:
            try:
                hh, mm = map(int, t.split(":"))
                c = now.replace(
                    hour=hh, minute=mm, second=0, microsecond=0)
                if c <= now:
                    c += timedelta(days=1)
                next_runs.append(c)
            except ValueError:
                pass
        if next_runs:
            nxt = min(next_runs)
            diff = nxt - now
            hrs  = int(diff.total_seconds() // 3600)
            mins = int((diff.total_seconds() % 3600) // 60)
            self.next_run_lbl.configure(
                text=("Next: " + nxt.strftime("%a %H:%M") +
                      "  (in " + str(hrs) + "h " + str(mins) + "m)"))

    def _add_sched_time(self):
        val = self.new_time_entry.get().strip()
        try:
            datetime.strptime(val, "%H:%M")
        except ValueError:
            messagebox.showerror(
                "Invalid Time",
                "Enter time as HH:MM (24h), e.g. 02:30",
                parent=self)
            return
        if val not in self._sched_times:
            self._sched_times.append(val)
            self._sched_times.sort()
        self.new_time_entry.delete(0, "end")
        self._rebuild_sched_list()

    # -------------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------------

    def _new_job(self):
        job = JobConfig(name="Job " + str(len(self.store.jobs) + 1))
        self.store.add(job)
        self.selected_index = len(self.store.jobs) - 1
        self._refresh_job_list()
        self._select_job(self.selected_index)

    def _remove_job(self):
        if self.selected_index is None:
            return
        job = self.store.jobs[self.selected_index]
        if not messagebox.askyesno(
            "Remove Job",
            "Remove '" + job.name + "'?", parent=self):
            return
        self.store.remove(self.selected_index)
        self.selected_index = None
        self._show_no_selection()
        self._refresh_job_list()
        self.scheduler.rebuild()
        self._append_log("Job removed.", "WARN")

    def _save_job(self):
        if self.selected_index is None:
            return
        old_path = self.store.jobs[self.selected_index].source_path
        job = self._collect_job_from_form()

        errors = []
        if job.ffs_exe and not os.path.isfile(job.ffs_exe):
            errors.append("FreeFileSync exe not found:\n" + job.ffs_exe)
        if job.batch_file and not os.path.isfile(job.batch_file):
            errors.append("Batch file not found:\n" + job.batch_file)
        if errors:
            messagebox.showerror(
                "Path Error",
                "\n\n".join(errors) +
                "\n\nPlease fix the paths before saving.",
                parent=self)
            return

        if job.source_path and not os.path.exists(job.source_path):
            if not messagebox.askyesno(
                "Source Path Warning",
                "Source path not currently reachable:\n" +
                job.source_path +
                "\n\nSave anyway?", parent=self):
                return

        self.store.update(self.selected_index, job)
        self.job_title_lbl.configure(text=job.name)
        self._refresh_job_list()
        self.scheduler.rebuild()
        self._update_cache_info(job)
        self._on_path_field_change()

        self.watch_info_lbl.configure(
            text="Full rescan mode  (every job run)")

        self._append_log("Job '" + job.name + "' saved.", "OK")

    def _run_current(self):
        if self.selected_index is None:
            return
        self._save_job()
        self._run_job_async(
            self.store.jobs[self.selected_index], "manual")

    def _run_all(self):
        if not self.store.jobs:
            messagebox.showinfo(
                "No Jobs", "Add at least one job first.", parent=self)
            return
        for job in self.store.jobs:
            if job.enabled:
                self._run_job_async(job, "manual")

    def _run_job_async(self, job: JobConfig,
                       triggered_by: str = "manual"):
        # Freeze settings for the complete run
        job = JobConfig.from_dict(job.to_dict())
        with self._state_lock:
            if job.job_id in self._scanning:
                self._append_log(
                    "Job '" + job.name + "' is already running.", "WARN")
                return
            self._scanning.add(job.job_id)
        self._set_job_status(job.name, "RUNNING")

        engine  = "parallel"
        workers = (
            job.num_workers if job.num_workers > 0
            else (ParallelScanner._AUTO_NETWORK
                  if _is_network_path(job.source_path)
                  else ParallelScanner._AUTO_LOCAL))

        is_selected = (
            self.selected_index is not None and
            self.store.jobs[self.selected_index].name == job.name)

        # Scanner control closures
        _scanner_ref: list = [None]

        def _do_pause(paused: bool):
            s = _scanner_ref[0]
            if s is None:
                return
            if paused:
                s.pause()
            else:
                s.resume()

        def _do_abort():
            s = _scanner_ref[0]
            if s is not None:
                s.stop()

        def _scanner_ready_cb(scanner):
            _scanner_ref[0] = scanner
            self.after(0, lambda:
                       self.scan_panel.bind_controls(_do_pause, _do_abort))

        # GUI: show progress panel THEN bind controls
        if is_selected:
            self.after(0, self._show_scan_progress)
            self.after(0, lambda e=engine, w=workers:
                       self.scan_panel.start(job.name, e, w))
            self.after(0, lambda:
                       self.scan_panel.bind_controls(
                           _do_pause, _do_abort))

        # Progress / override callbacks
        def _prog_cb(p):
            self.after(0, lambda pp=p: self.scan_panel.update(pp))

        def _override_cb(pct, total, changed):
            dialog_ready = threading.Event()
            dialog_holder = []

            def _show():
                try:
                    dialog_holder.append(ThresholdDialog(
                        self, job.name, pct,
                        job.threshold, total, changed))
                finally:
                    dialog_ready.set()

            self.after(0, _show)
            dialog_ready.wait()
            if not dialog_holder:
                return False
            dlg = dialog_holder[0]
            dlg.event.wait()
            return bool(dlg.proceed)

        # Worker thread
        _start_ts = [0.0]

        def _worker():
            _start_ts[0] = time.time()
            t0 = time.monotonic()
            status = "ERROR"
            guard = None

            try:
                guard = ChangeGuard(
                    job, self._append_log, _prog_cb,
                    _override_cb, _scanner_ready_cb)
                status = guard.check_and_run()
            except Exception as exc:
                self._append_log(
                    "Job '" + job.name + "' crashed: " + str(exc),
                    "ERROR")
            finally:
                with self._state_lock:
                    self._scanning.discard(job.job_id)

            # Reclaim scan objects before FreeFileSync launches
            try:
                import gc as _gc
                _gc.collect()
            except Exception:
                pass

            duration = time.monotonic() - t0

            # Record to history
            try:
                total   = getattr(guard, "_last_total",   0) or 0
                changed = getattr(guard, "_last_changed", 0) or 0
                pct     = getattr(guard, "_last_pct",     0.0) or 0.0
                hist = ScanHistory(job.job_id)
                hist.add({
                    "ts":           datetime.fromtimestamp(
                                        _start_ts[0])
                                    .strftime("%Y-%m-%d %H:%M:%S"),
                    "status":       status,
                    "total":        total,
                    "changed":      changed,
                    "pct":          pct,
                    "duration_s":   round(duration, 1),
                    "engine":       engine,
                    "triggered_by": triggered_by,
                })
            except Exception as exc:
                self._append_log(
                    "Could not record scan history: " + str(exc), "WARN")

            self.after(0, self.scan_panel.finish)
            self._set_job_status(job.name, status)

            def _after_scan():
                if (self.selected_index is not None and
                        self.store.jobs[self.selected_index].name ==
                        job.name):
                    self._update_cache_info(job)
                    self._refresh_history_tab(job.name)
                    self._show_detail()

            self.after(1200, _after_scan)

            if job.log_file:
                try:
                    Path(job.log_file).parent.mkdir(
                        parents=True, exist_ok=True)
                    with open(job.log_file, "a", encoding="utf-8") as lf:
                        lf.write(
                            "[" + datetime.now().strftime(
                                "%Y-%m-%d %H:%M:%S") +
                            "] Job '" + job.name +
                            "' finished: " + status + "\n")
                except Exception as exc:
                    self._append_log(
                        "Could not write log: " + str(exc), "WARN")

        threading.Thread(
            target=_worker, daemon=True, name="sg-job").start()

    # -------------------------------------------------------------------
    # Status
    # -------------------------------------------------------------------

    def _set_job_status(self, job_name: str, status: str):
        with self._state_lock:
            self._statuses[job_name] = status
            status_values = tuple(self._statuses.values())
        self.after(0, self._refresh_job_list)
        if self.selected_index is not None:
            if self.store.jobs[self.selected_index].name == job_name:
                self.after(0, lambda: self._set_status_badge(status))
        priority = ["ERROR", "ABORTED", "WARN", "RUNNING", "OK", "IDLE"]
        for p in priority:
            if p in status_values:
                self._update_tray_icon(p)
                break

    def _set_status_badge(self, status: str):
        bg = STATUS_COLORS.get(status, C_BORDER)
        fg = STATUS_TEXT_COLORS.get(status, C_MUTED)
        self.status_badge.configure(
            text="  " + status + "  ", fg_color=bg, text_color=fg)

    def _on_threshold(self, val):
        self.thr_lbl.configure(text=str(int(val)) + "%")

    def _on_hours(self, val):
        self.hrs_lbl.configure(text=str(int(val)) + "h")

    def _on_workers(self, val):
        self._update_workers_lbl(int(val))

    def _update_workers_lbl(self, val: int):
        if val == 0:
            self.wrk_lbl.configure(text="Auto")
            self.wrk_hint.configure(
                text=("0 = Auto: " +
                      str(ParallelScanner._AUTO_LOCAL) + " local / " +
                      str(ParallelScanner._AUTO_NETWORK) + " network"))
        else:
            self.wrk_lbl.configure(text=str(val))
            self.wrk_hint.configure(
                text="Higher = faster on network shares with latency")

    # -------------------------------------------------------------------
    # Log
    # -------------------------------------------------------------------

    def _append_log(self, message: str, level: str = "INFO"):
        """Thread-safe: may be called from any thread."""
        ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tag  = (level if level in ("INFO", "OK", "WARN", "ERROR", "ABORTED")
                else "INFO")
        line = ("[" + ts + "] [" + level.ljust(7) + "] " +
                message + "\n")
        self._log_queue.put((line, tag))

    def _drain_log_queue(self):
        """Called on the main thread every 200ms."""
        try:
            lines = []
            for _ in range(100):
                try:
                    lines.append(self._log_queue.get_nowait())
                except _queue.Empty:
                    break

            if lines:
                tb = self.log_box._textbox
                self.log_box.configure(state="normal")
                for line, tag in lines:
                    tb.insert("end", line, tag)
                total = int(tb.index("end-1c").split(".")[0])
                if total > LOG_MAX:
                    tb.delete("1.0", str(total - LOG_MAX) + ".0")
                self.log_box.configure(state="disabled")
                tb.see("end")
        except Exception:
            pass
        self.after(200, self._drain_log_queue)

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    # -------------------------------------------------------------------
    # Tray icon
    # -------------------------------------------------------------------

    def _build_tray_icon(self):
        img  = self._make_tray_image()
        menu = pystray.Menu(
            pystray.MenuItem("Show SyncGuard", self._tray_show,
                             default=True),
            pystray.MenuItem("Run All Jobs", self._tray_run_all),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._tray_quit),
        )
        self._tray = pystray.Icon(
            APP_NAME, img, APP_NAME + " " +
            __import__("syncguard").APP_VER, menu)
        threading.Thread(
            target=self._tray.run, daemon=True).start()

    def _make_tray_image(self, status: str = "IDLE") -> Image.Image:
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        dot_colors = {
            "OK":      (63, 185, 80, 255),
            "WARN":    (210, 153, 34, 255),
            "ERROR":   (248, 81, 73, 255),
            "ABORTED": (248, 81, 73, 255),
            "RUNNING": (56, 139, 253, 255),
            "IDLE":    (0, 212, 170, 255),
        }
        a = dot_colors.get(status, dot_colors["IDLE"])
        draw.ellipse([2, 2, 62, 62], fill=(22, 27, 34, 255))
        draw.arc([10, 10, 40, 40], start=30,  end=290, fill=a, width=5)
        draw.arc([24, 24, 54, 54], start=210, end=110, fill=a, width=5)
        draw.polygon([(38, 10), (46, 10), (42, 18)], fill=a)
        draw.polygon([(18, 54), (26, 54), (22, 46)], fill=a)
        return img

    def _update_tray_icon(self, status: str = "IDLE"):
        if hasattr(self, "_tray"):
            try:
                self._tray.icon = self._make_tray_image(status)
            except Exception:
                pass

    def _tray_show(self, i=None, it=None):
        self.after(0, self._restore_window)

    def _tray_run_all(self, i=None, it=None):
        self.after(0, self._run_all)

    def _tray_quit(self, i=None, it=None):
        self._quit_app()

    def _restore_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()
        self.state("normal")

    def _on_minimize(self, event=None):
        if self.state() == "iconic":
            self.after(50, self._hide_to_tray)

    def _hide_to_tray(self):
        self.withdraw()
        if hasattr(self, "_tray"):
            try:
                self._tray.notify(
                    "Running in background",
                    (APP_NAME + " is still running. "
                     "Right-click tray to manage."))
            except Exception:
                pass

    def _on_close(self):
        """Ask the user whether to minimize to tray or quit."""
        answer = messagebox.askyesnocancel(
            APP_NAME,
            "Minimize to system tray?\n\n"
            "Yes = minimize to tray (keeps running)\n"
            "No  = quit " + APP_NAME + "\n"
            "Cancel = do nothing",
            parent=self)
        if answer is True:       # Yes → minimize
            self._hide_to_tray()
        elif answer is False:    # No  → quit
            self._quit_app()
        # None (Cancel) → do nothing

    def _quit_app(self):
        self.scheduler.stop()
        self._stop_all_guardians()
        if hasattr(self, "_tray"):
            try:
                self._tray.stop()
            except Exception:
                pass
        self.after(0, self.destroy)
