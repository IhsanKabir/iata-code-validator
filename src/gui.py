"""Tkinter GUI for the IATA bulk validator.

Layout:
  ┌──────────────────────────────────────────────────────┐
  │ IATA CheckACode — Bulk Validator                     │
  ├──────────────────────────────────────────────────────┤
  │ Input file:  [path____________________] [Browse]     │
  │ Sheet:       [dropdown]                              │
  │ IATA column: [dropdown]                              │
  │ Row range:   [start] to [end]   (blank = all)        │
  │ Output dir:  [path____________________] [Browse]     │
  ├──────────────────────────────────────────────────────┤
  │ [Start]  [Pause]  [Resume]  [Stop]                   │
  ├──────────────────────────────────────────────────────┤
  │ Progress: ████████░░░  234 / 3000 (7.8%) — 4h 12m    │
  │                                                      │
  │ Log:                                                 │
  │ ┌──────────────────────────────────────────────────┐ │
  │ │ 14:02:11  32302491  VALID  TRAVEL POINT ...      │ │
  │ │ 14:02:14  32302492  INVALID                      │ │
  │ │ ...                                              │ │
  │ └──────────────────────────────────────────────────┘ │
  └──────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import tkinter as tk
import winsound
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__, auth, config, excel_io, updater
from .bd_agency_client import Agency, fetch_all_agencies, filter_status
from .bd_cache import BDAgencyCache
from .bd_matcher import (
    FIELD_ADDRESS,
    FIELD_LICENSE,
    FIELD_NAME,
    AgencyIndex,
)
from .cache import Cache
from .parser import LookupResult, now_iso
from .validator import (
    CaptchaChallenge,
    IATAValidator,
    ValidatorStopped,
    make_validator,
)

log = logging.getLogger(__name__)


# IATA worker → GUI message types
MSG_LOG = "log"
MSG_PROGRESS = "progress"
MSG_RESULT = "result"
MSG_CAPTCHA = "captcha"
MSG_DONE = "done"
MSG_ERROR = "error"

# BD Agency worker → GUI message types
MSG_BD_LOG = "bd_log"
MSG_BD_PROGRESS = "bd_progress"
MSG_BD_DONE = "bd_done"
MSG_BD_ERROR = "bd_error"
MSG_BD_REFRESHED = "bd_refreshed"   # payload: (count:int, last_refresh:str)

# Updater worker → GUI message types
MSG_UPDATE_LOG = "update_log"
MSG_UPDATE_PROGRESS = "update_progress"  # payload: (downloaded:int, total:int)
MSG_UPDATE_FOUND = "update_found"        # payload: UpdateInfo
MSG_UPDATE_NONE = "update_none"          # payload: UpdateInfo (or None)
MSG_UPDATE_DOWNLOADED = "update_downloaded"  # payload: Path
MSG_UPDATE_ERROR = "update_error"


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("IATA Code Validator")
        self.root.geometry("960x720")
        self.root.minsize(860, 620)

        # ----- IATA tab state -----
        self.input_path = tk.StringVar()
        self.sheet_name = tk.StringVar()
        self.column_name = tk.StringVar()
        self.start_row = tk.StringVar(value="2")
        self.end_row = tk.StringVar(value="")
        self.output_dir = tk.StringVar(value=str(Path.home() / "Documents"))

        self._sheet_columns: list[str] = []
        self._worker: threading.Thread | None = None
        self._validator: IATAValidator | None = None
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._stop_flag = threading.Event()
        self._captcha_clear_flag = threading.Event()

        # ----- BD tab state -----
        self.bd_mode = tk.StringVar(value="full")  # "full" or "lookup"
        self.bd_input_path = tk.StringVar()
        self.bd_sheet_name = tk.StringVar()
        self.bd_column_name = tk.StringVar()
        self.bd_output_dir = tk.StringVar(value=str(Path.home() / "Documents"))
        self.bd_include_expired = tk.BooleanVar(value=True)
        self.bd_match_name = tk.BooleanVar(value=True)       # default ON
        self.bd_match_license = tk.BooleanVar(value=True)    # default ON
        self.bd_match_address = tk.BooleanVar(value=False)   # default OFF

        self._bd_sheet_columns: list[str] = []
        self._bd_worker: threading.Thread | None = None
        self._bd_cache = BDAgencyCache(config.BD_CACHE_DB)

        # ----- Shared message queue -----
        self._msg_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()

        # ----- Auth + updater state -----
        self._signed_in_user: dict | None = None
        self._update_worker: threading.Thread | None = None

        self._build_ui()
        self._refresh_bd_status_label()
        self.root.after(100, self._poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # Bottom status bar — packed first so the Notebook fills the rest.
        status_frm = ttk.Frame(self.root, relief="sunken", borderwidth=1)
        status_frm.pack(side="bottom", fill="x")

        self.status_user_label = ttk.Label(status_frm, text="Not signed in")
        self.status_user_label.pack(side="left", padx=8, pady=2)

        self.status_version_label = ttk.Label(
            status_frm, text=f"v{__version__}", foreground="#475569"
        )
        self.status_version_label.pack(side="right", padx=(0, 8), pady=2)

        self.btn_check_updates = ttk.Button(
            status_frm,
            text="Check for updates",
            command=self._on_check_for_updates,
        )
        self.btn_check_updates.pack(side="right", padx=4, pady=2)

        self.btn_sign_out = ttk.Button(
            status_frm, text="Sign out", command=self._on_sign_out, state="disabled"
        )
        self.btn_sign_out.pack(side="right", padx=4, pady=2)

        # Wrap everything in a Notebook so each tool gets its own tab.
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        iata_tab = ttk.Frame(notebook)
        bd_tab = ttk.Frame(notebook)
        notebook.add(iata_tab, text="IATA Code Validator")
        notebook.add(bd_tab, text="BD Travel Agency Lookup")

        self._build_iata_tab(iata_tab)
        self._build_bd_tab(bd_tab)

    def _build_iata_tab(self, parent: ttk.Frame) -> None:
        pad = {"padx": 8, "pady": 4}

        # Input frame
        frm = ttk.LabelFrame(parent, text="Input")
        frm.pack(fill="x", padx=6, pady=(6, 4))

        ttk.Label(frm, text="Input Excel:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.input_path).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Browse...", command=self._pick_input).grid(row=0, column=2, **pad)

        ttk.Label(frm, text="Sheet:").grid(row=1, column=0, sticky="w", **pad)
        self.sheet_combo = ttk.Combobox(frm, textvariable=self.sheet_name, state="readonly")
        self.sheet_combo.grid(row=1, column=1, sticky="ew", **pad)
        self.sheet_combo.bind("<<ComboboxSelected>>", lambda _e: self._reload_columns())

        ttk.Label(frm, text="IATA column:").grid(row=2, column=0, sticky="w", **pad)
        self.col_combo = ttk.Combobox(frm, textvariable=self.column_name, state="readonly")
        self.col_combo.grid(row=2, column=1, sticky="ew", **pad)

        ttk.Label(frm, text="Row range:").grid(row=3, column=0, sticky="w", **pad)
        rr = ttk.Frame(frm)
        rr.grid(row=3, column=1, sticky="w", **pad)
        ttk.Label(rr, text="start").pack(side="left")
        ttk.Entry(rr, textvariable=self.start_row, width=8).pack(side="left", padx=(4, 12))
        ttk.Label(rr, text="end (blank=all)").pack(side="left")
        ttk.Entry(rr, textvariable=self.end_row, width=8).pack(side="left", padx=(4, 0))

        frm.columnconfigure(1, weight=1)

        # Output frame
        out = ttk.LabelFrame(parent, text="Output")
        out.pack(fill="x", padx=6, pady=4)
        ttk.Label(out, text="Folder:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(out, textvariable=self.output_dir).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(out, text="Browse...", command=self._pick_output).grid(row=0, column=2, **pad)
        out.columnconfigure(1, weight=1)

        # Controls
        ctrl = ttk.Frame(parent)
        ctrl.pack(fill="x", padx=6, pady=4)
        self.btn_start = ttk.Button(ctrl, text="Start", command=self._start)
        self.btn_start.pack(side="left", padx=4)
        self.btn_pause = ttk.Button(ctrl, text="Pause", command=self._pause, state="disabled")
        self.btn_pause.pack(side="left", padx=4)
        self.btn_resume = ttk.Button(ctrl, text="Resume", command=self._resume, state="disabled")
        self.btn_resume.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(ctrl, text="Stop", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=4)

        # Progress
        progress_frm = ttk.LabelFrame(parent, text="Progress")
        progress_frm.pack(fill="x", padx=6, pady=4)
        self.progress_bar = ttk.Progressbar(progress_frm, mode="determinate")
        self.progress_bar.pack(fill="x", padx=8, pady=6)
        self.progress_label = ttk.Label(progress_frm, text="Idle.")
        self.progress_label.pack(anchor="w", padx=8, pady=(0, 6))

        # Log
        log_frm = ttk.LabelFrame(parent, text="Log")
        log_frm.pack(fill="both", expand=True, padx=6, pady=(4, 6))
        self.log_text = tk.Text(log_frm, height=14, wrap="none", font=("Consolas", 9))
        self.log_text.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        scroll = ttk.Scrollbar(log_frm, command=self.log_text.yview)
        scroll.pack(side="right", fill="y", pady=8, padx=(0, 8))
        self.log_text.configure(yscrollcommand=scroll.set, state="disabled")

    # ------------------------------------------------------------------
    # BD Travel Agency tab
    # ------------------------------------------------------------------

    def _build_bd_tab(self, parent: ttk.Frame) -> None:
        pad = {"padx": 8, "pady": 4}

        # Step 1: Refresh / cache status
        refresh_frm = ttk.LabelFrame(parent, text="Step 1: Refresh data from regtravelagency.gov.bd")
        refresh_frm.pack(fill="x", padx=6, pady=(6, 4))
        self.bd_status_label = ttk.Label(refresh_frm, text="…", justify="left")
        self.bd_status_label.pack(anchor="w", padx=8, pady=(8, 4))
        self.btn_bd_refresh = ttk.Button(
            refresh_frm,
            text="🔄 Refresh now",
            command=self._bd_refresh,
        )
        self.btn_bd_refresh.pack(anchor="w", padx=8, pady=(0, 8))

        # Step 2: Mode
        mode_frm = ttk.LabelFrame(parent, text="Step 2: Choose what to do")
        mode_frm.pack(fill="x", padx=6, pady=4)
        ttk.Radiobutton(
            mode_frm,
            text="Export FULL list to Excel (all cached agencies)",
            variable=self.bd_mode,
            value="full",
            command=self._toggle_bd_mode,
        ).pack(anchor="w", padx=8, pady=(8, 2))
        ttk.Radiobutton(
            mode_frm,
            text="Lookup names from Excel (match each name against the cached list)",
            variable=self.bd_mode,
            value="lookup",
            command=self._toggle_bd_mode,
        ).pack(anchor="w", padx=8, pady=(2, 8))

        # Step 3: input file picker (only enabled in lookup mode)
        self.bd_input_frm = ttk.LabelFrame(parent, text="Step 3: Input Excel (lookup mode only)")
        self.bd_input_frm.pack(fill="x", padx=6, pady=4)
        ttk.Label(self.bd_input_frm, text="File:").grid(row=0, column=0, sticky="w", **pad)
        self.bd_input_entry = ttk.Entry(self.bd_input_frm, textvariable=self.bd_input_path)
        self.bd_input_entry.grid(row=0, column=1, sticky="ew", **pad)
        self.bd_input_btn = ttk.Button(
            self.bd_input_frm, text="Browse...", command=self._bd_pick_input
        )
        self.bd_input_btn.grid(row=0, column=2, **pad)

        ttk.Label(self.bd_input_frm, text="Sheet:").grid(row=1, column=0, sticky="w", **pad)
        self.bd_sheet_combo = ttk.Combobox(
            self.bd_input_frm, textvariable=self.bd_sheet_name, state="readonly"
        )
        self.bd_sheet_combo.grid(row=1, column=1, sticky="ew", **pad)
        self.bd_sheet_combo.bind("<<ComboboxSelected>>", lambda _e: self._bd_reload_columns())

        ttk.Label(self.bd_input_frm, text="Name / License # column:").grid(
            row=2, column=0, sticky="w", **pad
        )
        self.bd_col_combo = ttk.Combobox(
            self.bd_input_frm, textvariable=self.bd_column_name, state="readonly"
        )
        self.bd_col_combo.grid(row=2, column=1, sticky="ew", **pad)
        self.bd_input_frm.columnconfigure(1, weight=1)

        # Match against (lookup mode only) — three independent checkboxes.
        # Default Name+License preserves v1.1.0 behaviour; tick Address
        # to also match against the agency's full address.
        match_frm = ttk.LabelFrame(
            parent, text="Match against (lookup mode only)"
        )
        match_frm.pack(fill="x", padx=6, pady=4)
        ttk.Checkbutton(
            match_frm, text="Agency Name", variable=self.bd_match_name,
        ).pack(side="left", padx=8, pady=8)
        ttk.Checkbutton(
            match_frm, text="License Number", variable=self.bd_match_license,
        ).pack(side="left", padx=8, pady=8)
        ttk.Checkbutton(
            match_frm, text="Address", variable=self.bd_match_address,
        ).pack(side="left", padx=8, pady=8)

        # Filter
        filter_frm = ttk.LabelFrame(parent, text="Filter")
        filter_frm.pack(fill="x", padx=6, pady=4)
        ttk.Checkbutton(
            filter_frm,
            text="Include EXPIRED-PENDING agencies (license expired but still in active list)",
            variable=self.bd_include_expired,
        ).pack(anchor="w", padx=8, pady=8)

        # Output
        out_frm = ttk.LabelFrame(parent, text="Output")
        out_frm.pack(fill="x", padx=6, pady=4)
        ttk.Label(out_frm, text="Folder:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(out_frm, textvariable=self.bd_output_dir).grid(
            row=0, column=1, sticky="ew", **pad
        )
        ttk.Button(out_frm, text="Browse...", command=self._bd_pick_output).grid(
            row=0, column=2, **pad
        )
        out_frm.columnconfigure(1, weight=1)

        # Run
        run_frm = ttk.Frame(parent)
        run_frm.pack(fill="x", padx=6, pady=4)
        self.btn_bd_run = ttk.Button(run_frm, text="Run", command=self._bd_run)
        self.btn_bd_run.pack(side="left", padx=4)

        # Progress
        bd_progress_frm = ttk.LabelFrame(parent, text="Progress")
        bd_progress_frm.pack(fill="x", padx=6, pady=4)
        self.bd_progress_bar = ttk.Progressbar(bd_progress_frm, mode="determinate")
        self.bd_progress_bar.pack(fill="x", padx=8, pady=6)
        self.bd_progress_label = ttk.Label(bd_progress_frm, text="Idle.")
        self.bd_progress_label.pack(anchor="w", padx=8, pady=(0, 6))

        # Log
        bd_log_frm = ttk.LabelFrame(parent, text="Log")
        bd_log_frm.pack(fill="both", expand=True, padx=6, pady=(4, 6))
        self.bd_log_text = tk.Text(bd_log_frm, height=10, wrap="none", font=("Consolas", 9))
        self.bd_log_text.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        bd_scroll = ttk.Scrollbar(bd_log_frm, command=self.bd_log_text.yview)
        bd_scroll.pack(side="right", fill="y", pady=8, padx=(0, 8))
        self.bd_log_text.configure(yscrollcommand=bd_scroll.set, state="disabled")

        self._toggle_bd_mode()

    # ------------------------------------------------------------------
    # File pickers
    # ------------------------------------------------------------------

    def _pick_input(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Excel with IATA numbers",
            filetypes=[("Excel files", "*.xlsx *.xlsm"), ("All files", "*.*")],
        )
        if not path:
            return
        self.input_path.set(path)
        self._reload_sheets()

    def _pick_output(self) -> None:
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            self.output_dir.set(path)

    def _reload_sheets(self) -> None:
        try:
            sheets = excel_io.list_sheet_names(Path(self.input_path.get()))
        except Exception as e:
            messagebox.showerror("Cannot read Excel", str(e))
            return
        self.sheet_combo["values"] = sheets
        if sheets:
            self.sheet_combo.current(0)
            self._reload_columns()

    def _reload_columns(self) -> None:
        try:
            cols = excel_io.list_columns(Path(self.input_path.get()), self.sheet_name.get())
        except Exception as e:
            messagebox.showerror("Cannot read columns", str(e))
            return
        self._sheet_columns = cols
        self.col_combo["values"] = cols
        # Try to auto-pick a column whose name looks like IATA
        guess = next(
            (c for c in cols if "iata" in c.lower() or "code" in c.lower() or "number" in c.lower()),
            cols[0] if cols else "",
        )
        self.column_name.set(guess)

    # ------------------------------------------------------------------
    # Run control
    # ------------------------------------------------------------------

    def _start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        cfg = self._validate_inputs()
        if cfg is None:
            return

        self._stop_flag.clear()
        self._pause_event.set()
        self._captcha_clear_flag.clear()

        self.btn_start.configure(state="disabled")
        self.btn_pause.configure(state="normal")
        self.btn_stop.configure(state="normal")

        self._worker = threading.Thread(
            target=self._run_worker,
            args=(cfg,),
            daemon=True,
        )
        self._worker.start()

    def _pause(self) -> None:
        self._pause_event.clear()
        self.btn_pause.configure(state="disabled")
        self.btn_resume.configure(state="normal")
        self._log("Paused. Click Resume when ready.")

    def _resume(self) -> None:
        self._pause_event.set()
        self._captcha_clear_flag.set()
        self.btn_pause.configure(state="normal")
        self.btn_resume.configure(state="disabled")
        self._log("Resumed.")

    def _stop(self) -> None:
        if not messagebox.askyesno("Stop?", "Stop the run? Partial results are already saved."):
            return
        self._stop_flag.set()
        self._pause_event.set()  # un-pause so the worker exits its wait
        self._captcha_clear_flag.set()
        if self._validator is not None:
            self._validator.stop()

    def _on_close(self) -> None:
        any_alive = (
            (self._worker is not None and self._worker.is_alive())
            or (self._bd_worker is not None and self._bd_worker.is_alive())
        )
        if any_alive:
            if not messagebox.askyesno("Quit?", "A run is in progress. Quit anyway?"):
                return
            self._stop_flag.set()
            self._pause_event.set()
            self._captcha_clear_flag.set()
            if self._validator is not None:
                self._validator.stop()
            if self._worker is not None:
                self._worker.join(timeout=5)
            # BD worker is HTTP-only and short-lived; just let it die.
        self.root.destroy()

    def _validate_inputs(self) -> dict | None:
        input_path = self.input_path.get().strip()
        if not input_path or not Path(input_path).exists():
            messagebox.showerror("Invalid input", "Pick a valid Excel file.")
            return None
        if not self.sheet_name.get():
            messagebox.showerror("Invalid input", "Pick a sheet.")
            return None
        if not self.column_name.get() or self.column_name.get() not in self._sheet_columns:
            messagebox.showerror("Invalid input", "Pick the IATA column.")
            return None
        try:
            start = int(self.start_row.get())
        except ValueError:
            messagebox.showerror("Invalid input", "Start row must be an integer.")
            return None
        if start < 2:
            messagebox.showerror("Invalid input", "Start row must be >= 2 (row 1 = header).")
            return None
        end_str = self.end_row.get().strip()
        end: int | None = None
        if end_str:
            try:
                end = int(end_str)
            except ValueError:
                messagebox.showerror("Invalid input", "End row must be empty or an integer.")
                return None
            if end < start:
                messagebox.showerror("Invalid input", "End row must be >= start row.")
                return None
        out_dir = Path(self.output_dir.get())
        if not out_dir.exists():
            messagebox.showerror("Invalid output", "Output folder does not exist.")
            return None

        column_index = self._sheet_columns.index(self.column_name.get())
        return {
            "input_path": Path(input_path),
            "sheet": self.sheet_name.get(),
            "column_index": column_index,
            "start_row": start,
            "end_row": end,
            "output_dir": out_dir,
        }

    # ------------------------------------------------------------------
    # Worker thread
    # ------------------------------------------------------------------

    def _run_worker(self, cfg: dict) -> None:
        try:
            self._post(MSG_LOG, "Reading IATA numbers from Excel...")
            rows = excel_io.read_iata_numbers(
                cfg["input_path"],
                cfg["sheet"],
                cfg["column_index"],
                cfg["start_row"],
                cfg["end_row"],
            )
            if not rows:
                self._post(MSG_ERROR, "No IATA numbers found in the selected range.")
                return

            output_path = excel_io.build_output_path(cfg["output_dir"])
            self._post(MSG_LOG, f"Writing results to: {output_path}")

            cache = Cache(config.CACHE_DB)
            self._post(MSG_LOG, f"Cache: {cache.count()} prior lookups available.")

            total = len(rows)
            self._post(MSG_PROGRESS, (0, total, "Starting browser..."))

            # ResultWriter as context manager guarantees the final flush even
            # if the validator raises during shutdown.
            with excel_io.ResultWriter(output_path) as writer, make_validator(
                profile_dir=config.PROFILE_DIR,
                on_log=lambda m: self._post(MSG_LOG, m),
            ) as validator:
                self._validator = validator
                started = time.monotonic()

                for idx, (excel_row, iata) in enumerate(rows, start=1):
                    if self._stop_flag.is_set():
                        self._post(MSG_LOG, "Stop requested — exiting.")
                        break
                    # Honor pause
                    self._pause_event.wait()

                    # Cache hit
                    cached = cache.get(iata)
                    if cached is not None:
                        writer.append(cached)
                        self._post(MSG_RESULT, cached)
                        self._post_progress(idx, total, started, "cached")
                        continue

                    result = self._lookup_with_retry(validator, iata)
                    cache.put(result)
                    writer.append(result)
                    self._post(MSG_RESULT, result)
                    self._post_progress(idx, total, started, result.status)

            self._post(MSG_LOG, f"Done. Output: {output_path}")
            self._post(MSG_DONE, str(output_path))

            # Best-effort usage log. Never breaks the run on failure.
            try:
                auth.log_lookup_event(
                    action="iata_validate",
                    target=str(cfg["input_path"].name),
                    count=total,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("usage log failed: %s", exc)

        except Exception as e:
            log.exception("worker crashed")
            self._post(MSG_ERROR, f"{type(e).__name__}: {e}")
        finally:
            self._validator = None

    def _lookup_with_retry(self, validator: IATAValidator, iata: str) -> LookupResult:
        """One CAPTCHA retry — alert user, wait for green check, then resume.

        Uses complete_after_captcha (NOT lookup) on the second pass so the
        user's puzzle solve isn't thrown away by a re-navigation.
        """
        try:
            return validator.lookup(iata)
        except CaptchaChallenge as e:
            self._post(MSG_CAPTCHA, str(e))
            self._captcha_clear_flag.clear()
            ok = validator.wait_for_user_captcha()
            if not ok:
                return LookupResult(
                    iata_number=iata,
                    trading_name="",
                    country="",
                    accredited="",
                    status="ERROR",
                    checked_at=now_iso(),
                    notes="captcha not solved within timeout",
                )
            # User solved it — resume from the submit step on the SAME page.
            try:
                return validator.complete_after_captcha(iata)
            except (CaptchaChallenge, ValidatorStopped):
                raise
            except Exception as e2:  # noqa: BLE001 — keep loop alive on unknown errors
                log.warning("resume failed for %s: %s", iata, e2)
                return LookupResult(
                    iata_number=iata,
                    trading_name="",
                    country="",
                    accredited="",
                    status="ERROR",
                    checked_at=now_iso(),
                    notes=f"resume failed: {e2}",
                )
        except ValidatorStopped:
            raise

    # ------------------------------------------------------------------
    # Worker → GUI messaging
    # ------------------------------------------------------------------

    def _post(self, kind: str, payload: object) -> None:
        self._msg_queue.put((kind, payload))

    def _post_progress(
        self, idx: int, total: int, started: float, last_status: str
    ) -> None:
        elapsed = max(time.monotonic() - started, 0.001)
        rate = idx / elapsed
        remaining = (total - idx) / rate if rate > 0 else 0
        msg = (
            f"{idx} / {total} ({100 * idx / total:.1f}%)  "
            f"— last: {last_status}  — ETA: {_fmt_dur(remaining)}"
        )
        self._post(MSG_PROGRESS, (idx, total, msg))

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._msg_queue.get_nowait()
                self._handle_msg(kind, payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _handle_msg(self, kind: str, payload: object) -> None:
        if kind == MSG_LOG:
            self._log(str(payload))
        elif kind == MSG_PROGRESS:
            idx, total, msg = payload  # type: ignore[misc]
            self.progress_bar["maximum"] = total
            self.progress_bar["value"] = idx
            self.progress_label.configure(text=msg)
        elif kind == MSG_RESULT:
            r: LookupResult = payload  # type: ignore[assignment]
            line = (
                f"{r.checked_at}  {r.iata_number:<12}  {r.status:<8}  "
                f"{r.trading_name}  ({r.country})"
            )
            self._log(line)
        elif kind == MSG_CAPTCHA:
            self._on_captcha_alert(str(payload))
        elif kind == MSG_DONE:
            self._log(f"Finished. File: {payload}")
            messagebox.showinfo("Done", f"Finished.\n\nOutput:\n{payload}")
            self._reset_buttons()
        elif kind == MSG_ERROR:
            self._log(f"ERROR: {payload}")
            messagebox.showerror("Error", str(payload))
            self._reset_buttons()
        # ------ BD tab messages ------
        elif kind == MSG_BD_LOG:
            self._bd_log(str(payload))
        elif kind == MSG_BD_PROGRESS:
            idx, total, msg = payload  # type: ignore[misc]
            self.bd_progress_bar["maximum"] = total
            self.bd_progress_bar["value"] = idx
            self.bd_progress_label.configure(text=msg)
        elif kind == MSG_BD_REFRESHED:
            count, last_refresh = payload  # type: ignore[misc]
            self._bd_log(f"Refreshed: {count:,} agencies cached at {last_refresh}.")
            self._refresh_bd_status_label()
            self._bd_reset_buttons()
        elif kind == MSG_BD_DONE:
            path = str(payload)
            self._bd_log(f"Done. File: {path}")
            messagebox.showinfo("BD Agency Lookup — Done", f"Finished.\n\nOutput:\n{path}")
            self._bd_reset_buttons()
        elif kind == MSG_BD_ERROR:
            self._bd_log(f"ERROR: {payload}")
            messagebox.showerror("BD Agency Lookup — Error", str(payload))
            self._bd_reset_buttons()
        # ------ Update messages ------
        elif kind == MSG_UPDATE_FOUND:
            self._on_update_found(payload)  # type: ignore[arg-type]
        elif kind == MSG_UPDATE_NONE:
            self.btn_check_updates.configure(state="normal", text="Check for updates")
            messagebox.showinfo(
                "Up to date",
                f"You're on the latest version (v{__version__}).",
            )
        elif kind == MSG_UPDATE_PROGRESS:
            downloaded, total = payload  # type: ignore[misc]
            if total > 0:
                pct = 100 * downloaded / total
                self.btn_check_updates.configure(
                    text=f"Downloading… {pct:.0f}%"
                )
            else:
                mb = downloaded / 1_000_000
                self.btn_check_updates.configure(text=f"Downloading… {mb:.0f} MB")
        elif kind == MSG_UPDATE_DOWNLOADED:
            self._on_update_downloaded(str(payload))
        elif kind == MSG_UPDATE_ERROR:
            self.btn_check_updates.configure(state="normal", text="Check for updates")
            messagebox.showerror("Update", str(payload))

    def _on_captcha_alert(self, message: str) -> None:
        self._log(f"CAPTCHA: {message}")
        try:
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            for _ in range(2):
                winsound.Beep(880, 250)
                time.sleep(0.05)
        except Exception:
            pass
        # Bring window to front to nudge the user
        try:
            self.root.attributes("-topmost", True)
            self.root.after(100, lambda: self.root.attributes("-topmost", False))
            self.root.bell()
        except Exception:
            pass
        self.progress_label.configure(text="⚠ CAPTCHA — solve it in the browser, then keep working.")

    def _reset_buttons(self) -> None:
        self.btn_start.configure(state="normal")
        self.btn_pause.configure(state="disabled")
        self.btn_resume.configure(state="disabled")
        self.btn_stop.configure(state="disabled")

    def _log(self, msg: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ==================================================================
    # BD Travel Agency tab — actions + worker
    # ==================================================================

    def _refresh_bd_status_label(self) -> None:
        """Update the 'Last fetched: ...' label using the cache."""
        last = self._bd_cache.last_refresh()
        count = self._bd_cache.count()
        if not last or count == 0:
            self.bd_status_label.configure(
                text="No cached data yet. Click Refresh to download the agency list."
            )
            return
        self.bd_status_label.configure(
            text=f"Last fetched: {last}  ·  {count:,} cached records"
        )

    def _toggle_bd_mode(self) -> None:
        """Enable/disable input pickers based on current mode."""
        is_lookup = self.bd_mode.get() == "lookup"
        state = "normal" if is_lookup else "disabled"
        for widget in (
            self.bd_input_entry,
            self.bd_input_btn,
            self.bd_sheet_combo,
            self.bd_col_combo,
        ):
            widget.configure(state=state if widget is not self.bd_sheet_combo and widget is not self.bd_col_combo else ("readonly" if is_lookup else "disabled"))

    def _bd_pick_input(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Excel with agency names",
            filetypes=[("Excel files", "*.xlsx *.xlsm"), ("All files", "*.*")],
        )
        if not path:
            return
        self.bd_input_path.set(path)
        self._bd_reload_sheets()

    def _bd_pick_output(self) -> None:
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            self.bd_output_dir.set(path)

    def _bd_reload_sheets(self) -> None:
        try:
            sheets = excel_io.list_sheet_names(Path(self.bd_input_path.get()))
        except Exception as e:
            messagebox.showerror("Cannot read Excel", str(e))
            return
        self.bd_sheet_combo["values"] = sheets
        if sheets:
            self.bd_sheet_combo.current(0)
            self._bd_reload_columns()

    def _bd_reload_columns(self) -> None:
        try:
            cols = excel_io.list_columns(
                Path(self.bd_input_path.get()), self.bd_sheet_name.get()
            )
        except Exception as e:
            messagebox.showerror("Cannot read columns", str(e))
            return
        self._bd_sheet_columns = cols
        self.bd_col_combo["values"] = cols
        guess = next(
            (c for c in cols if "name" in c.lower() or "agency" in c.lower() or "license" in c.lower()),
            cols[0] if cols else "",
        )
        self.bd_column_name.set(guess)

    # ------------------------------------------------------------------
    # Refresh worker
    # ------------------------------------------------------------------

    def _bd_refresh(self) -> None:
        if self._bd_worker is not None and self._bd_worker.is_alive():
            messagebox.showinfo("Busy", "Another BD task is already running.")
            return
        self.btn_bd_refresh.configure(state="disabled")
        self.btn_bd_run.configure(state="disabled")
        self._post(MSG_BD_LOG, "Refreshing agency list from regtravelagency.gov.bd ...")
        self._bd_worker = threading.Thread(
            target=self._bd_run_refresh,
            daemon=True,
        )
        self._bd_worker.start()

    def _bd_run_refresh(self) -> None:
        try:
            agencies = fetch_all_agencies()
            self._bd_cache.replace_all(agencies)
            self._post(
                MSG_BD_REFRESHED,
                (len(agencies), self._bd_cache.last_refresh()),
            )
            try:
                auth.log_lookup_event(
                    action="bd_refresh",
                    target="get-list",
                    count=len(agencies),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("usage log failed: %s", exc)
        except Exception as e:
            log.exception("BD refresh failed")
            self._post(MSG_BD_ERROR, f"Refresh failed: {type(e).__name__}: {e}")

    # ------------------------------------------------------------------
    # Run worker (full export OR lookup)
    # ------------------------------------------------------------------

    def _bd_run(self) -> None:
        if self._bd_worker is not None and self._bd_worker.is_alive():
            messagebox.showinfo("Busy", "Another BD task is already running.")
            return
        if self._bd_cache.count() == 0:
            messagebox.showerror(
                "No cached data",
                "Click Refresh first to download the agency list.",
            )
            return

        out_dir = Path(self.bd_output_dir.get())
        if not out_dir.exists():
            messagebox.showerror("Invalid output", "Output folder does not exist.")
            return

        mode = self.bd_mode.get()
        cfg: dict = {
            "mode": mode,
            "output_dir": out_dir,
            "include_expired": bool(self.bd_include_expired.get()),
        }
        if mode == "lookup":
            input_path = self.bd_input_path.get().strip()
            if not input_path or not Path(input_path).exists():
                messagebox.showerror("Invalid input", "Pick a valid Excel file.")
                return
            if not self.bd_sheet_name.get():
                messagebox.showerror("Invalid input", "Pick a sheet.")
                return
            if (
                not self.bd_column_name.get()
                or self.bd_column_name.get() not in self._bd_sheet_columns
            ):
                messagebox.showerror("Invalid input", "Pick the column.")
                return

            # Field selector — pick the checked ones in priority order.
            fields: list[str] = []
            if self.bd_match_name.get():
                fields.append(FIELD_NAME)
            if self.bd_match_license.get():
                fields.append(FIELD_LICENSE)
            if self.bd_match_address.get():
                fields.append(FIELD_ADDRESS)
            if not fields:
                messagebox.showerror(
                    "Invalid input",
                    "Tick at least one field to match against "
                    "(Agency Name / License Number / Address).",
                )
                return

            cfg["input_path"] = Path(input_path)
            cfg["sheet"] = self.bd_sheet_name.get()
            cfg["column_index"] = self._bd_sheet_columns.index(self.bd_column_name.get())
            cfg["match_fields"] = tuple(fields)

        self.btn_bd_refresh.configure(state="disabled")
        self.btn_bd_run.configure(state="disabled")
        self._bd_worker = threading.Thread(
            target=self._bd_worker_run,
            args=(cfg,),
            daemon=True,
        )
        self._bd_worker.start()

    def _bd_worker_run(self, cfg: dict) -> None:
        try:
            agencies = self._bd_cache.all()
            agencies = filter_status(agencies, cfg["include_expired"])
            self._post(MSG_BD_LOG, f"Loaded {len(agencies):,} cached agencies after filter.")

            if cfg["mode"] == "full":
                output_path = excel_io.build_bd_output_path(cfg["output_dir"], kind="full")
                self._post(MSG_BD_LOG, f"Writing full list to {output_path} ...")
                excel_io.write_bd_full_list(output_path, agencies)
                self._post(MSG_BD_DONE, str(output_path))
                try:
                    auth.log_lookup_event(
                        action="bd_export",
                        target="full-list",
                        count=len(agencies),
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("usage log failed: %s", exc)
                return

            # Lookup mode
            inputs = excel_io.read_iata_numbers(  # reuses generic Excel reader
                cfg["input_path"],
                cfg["sheet"],
                cfg["column_index"],
                start_row=2,
                end_row=None,
            )
            # read_iata_numbers strips and normalises — but BD names need
            # less normalisation. Re-read raw values so names like
            # "ZEPHYR TOURS & TRAVELS" survive.
            inputs = self._read_bd_inputs(
                cfg["input_path"], cfg["sheet"], cfg["column_index"]
            )
            if not inputs:
                self._post(MSG_BD_ERROR, "No values found in the selected column.")
                return

            self._post(MSG_BD_LOG, f"Building search index over {len(agencies):,} agencies ...")
            index = AgencyIndex(agencies)

            fields = cfg.get("match_fields") or (FIELD_NAME, FIELD_LICENSE)
            self._post(
                MSG_BD_LOG,
                f"Matching {len(inputs):,} input rows against fields: {', '.join(fields)} ...",
            )
            results = []
            total = len(inputs)
            for i, (_, value) in enumerate(inputs, start=1):
                results.append(index.lookup(value, fields=fields))
                if i % 50 == 0 or i == total:
                    self._post(MSG_BD_PROGRESS, (i, total, f"{i}/{total} matched"))

            output_path = excel_io.build_bd_output_path(cfg["output_dir"], kind="lookup")
            self._post(MSG_BD_LOG, f"Writing lookup results to {output_path} ...")
            excel_io.write_bd_lookup_results(output_path, results)

            # Summary
            counts: dict[str, int] = {}
            for r in results:
                counts[r.match_method] = counts.get(r.match_method, 0) + 1
            summary = ", ".join(
                f"{k}={v}"
                for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
            )
            self._post(MSG_BD_LOG, f"Match summary: {summary}")
            self._post(MSG_BD_DONE, str(output_path))

            try:
                auth.log_lookup_event(
                    action="bd_lookup",
                    target=str(cfg["input_path"].name),
                    count=len(inputs),
                    notes="fields=" + "/".join(fields),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("usage log failed: %s", exc)

        except Exception as e:
            log.exception("BD worker crashed")
            self._post(MSG_BD_ERROR, f"{type(e).__name__}: {e}")

    @staticmethod
    def _read_bd_inputs(
        path: Path, sheet: str, column_index: int
    ) -> list[tuple[int, str]]:
        """Read a column of free-form text from Excel.

        Unlike `read_iata_numbers`, we don't strip hyphens/spaces — agency
        names need to keep their formatting for matching.
        """
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb[sheet]
            rows: list[tuple[int, str]] = []
            for row_idx, row in enumerate(
                ws.iter_rows(min_row=2, values_only=True), start=2
            ):
                if column_index >= len(row):
                    continue
                value = row[column_index]
                if value is None:
                    continue
                text = str(value).strip()
                if not text:
                    continue
                rows.append((row_idx, text))
            return rows
        finally:
            wb.close()

    # ------------------------------------------------------------------
    # BD message handling
    # ------------------------------------------------------------------

    def _bd_log(self, msg: str) -> None:
        self.bd_log_text.configure(state="normal")
        self.bd_log_text.insert("end", msg + "\n")
        self.bd_log_text.see("end")
        self.bd_log_text.configure(state="disabled")

    def _bd_reset_buttons(self) -> None:
        self.btn_bd_refresh.configure(state="normal")
        self.btn_bd_run.configure(state="normal")

    # ==================================================================
    # Auth + status bar
    # ==================================================================

    def ensure_signed_in(self) -> bool:
        """Show the login dialog if no valid session, return True on success."""
        # 1. If we already have a token, validate it server-side.
        if auth.is_signed_in():
            user = auth.whoami()
            if user:
                self._signed_in_user = user
                self._refresh_user_label()
                return True
            # Stale token — drop it and prompt fresh sign-in.
            auth.clear_token()

        # 2. Show modal sign-in dialog.
        return self._show_login_dialog()

    def _show_login_dialog(self) -> bool:
        dlg = tk.Toplevel(self.root)
        dlg.title("Sign in — IATA Code Validator")
        dlg.geometry("480x230")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.protocol("WM_DELETE_WINDOW", lambda: dlg.destroy())

        ttk.Label(
            dlg,
            text="Sign in to continue",
            font=("Segoe UI", 14, "bold"),
        ).pack(pady=(20, 6), padx=16)
        ttk.Label(
            dlg,
            text=(
                "The IATA Code Validator is licensed to your team. "
                "Click below and complete the Google sign-in in your browser."
            ),
            wraplength=440,
            justify="left",
        ).pack(padx=16, pady=(0, 16))

        status_var = tk.StringVar(value="")
        status_label = ttk.Label(dlg, textvariable=status_var, foreground="#b91c1c")
        status_label.pack(padx=16)

        result = {"ok": False}

        def do_signin() -> None:
            btn_signin.configure(state="disabled", text="Opening browser…")
            status_var.set("")
            dlg.update_idletasks()
            try:
                oauth_result = auth.run_google_oauth_flow()
                auth.save_token(oauth_result.session_token)
                self._signed_in_user = {
                    "email": oauth_result.email,
                    "full_name": oauth_result.name,
                    "user_id": oauth_result.user_id,
                }
                result["ok"] = True
                dlg.destroy()
            except auth.AuthError as exc:
                status_var.set(str(exc))
                btn_signin.configure(state="normal", text="Sign in with Google")
            except Exception as exc:  # noqa: BLE001
                status_var.set(f"Unexpected error: {exc}")
                btn_signin.configure(state="normal", text="Sign in with Google")

        btn_signin = ttk.Button(dlg, text="Sign in with Google", command=do_signin)
        btn_signin.pack(pady=(12, 8))

        ttk.Button(dlg, text="Quit", command=dlg.destroy).pack()

        self.root.wait_window(dlg)
        if result["ok"]:
            self._refresh_user_label()
        return result["ok"]

    def _refresh_user_label(self) -> None:
        if self._signed_in_user:
            email = self._signed_in_user.get("email") or "(signed in)"
            self.status_user_label.configure(text=f"Signed in: {email}")
            self.btn_sign_out.configure(state="normal")
        else:
            self.status_user_label.configure(text="Not signed in")
            self.btn_sign_out.configure(state="disabled")

    def _on_sign_out(self) -> None:
        if not messagebox.askyesno(
            "Sign out?",
            "Sign out of the IATA Code Validator? You will need to sign in again on next launch.",
        ):
            return
        try:
            auth.logout()
        except Exception as exc:  # noqa: BLE001
            log.warning("logout error: %s", exc)
        self._signed_in_user = None
        self._refresh_user_label()
        messagebox.showinfo("Signed out", "Signed out. Please restart the app.")

    # ==================================================================
    # Self-updater
    # ==================================================================

    def _on_check_for_updates(self) -> None:
        if self._update_worker is not None and self._update_worker.is_alive():
            return
        self.btn_check_updates.configure(state="disabled", text="Checking…")
        self._update_worker = threading.Thread(
            target=self._update_check_worker, daemon=True
        )
        self._update_worker.start()

    def _update_check_worker(self) -> None:
        info = updater.check_for_update()
        if info is None:
            self._post(MSG_UPDATE_ERROR, "Could not check for updates (network error or no published release).")
            return
        if info.is_newer:
            self._post(MSG_UPDATE_FOUND, info)
        else:
            self._post(MSG_UPDATE_NONE, info)

    def _on_update_found(self, info: "updater.UpdateInfo") -> None:
        self.btn_check_updates.configure(state="normal", text="Check for updates")
        msg = (
            f"A new version is available.\n\n"
            f"Current : v{__version__}\n"
            f"Latest  : v{info.latest_version}\n\n"
            f"Download (~380 MB) and restart now?"
        )
        if not messagebox.askyesno("Update available", msg):
            return
        self.btn_check_updates.configure(state="disabled", text="Downloading…")
        self._update_worker = threading.Thread(
            target=self._update_download_worker,
            args=(info.download_url,),
            daemon=True,
        )
        self._update_worker.start()

    def _update_download_worker(self, url: str) -> None:
        def progress(downloaded: int, total: int) -> None:
            self._post(MSG_UPDATE_PROGRESS, (downloaded, total))
        try:
            staged = updater.download_update(url, on_progress=progress)
            self._post(MSG_UPDATE_DOWNLOADED, str(staged))
        except Exception as exc:  # noqa: BLE001
            log.exception("update download failed")
            self._post(MSG_UPDATE_ERROR, f"Download failed: {exc}")

    def _on_update_downloaded(self, staged_path: str) -> None:
        self.btn_check_updates.configure(text="Restart to update", state="normal")
        if not messagebox.askyesno(
            "Download complete",
            "The new version is ready. The app will close and the new version will launch automatically. Continue?",
        ):
            return
        ok = updater.apply_update_and_exit()
        if not ok:
            messagebox.showwarning(
                "Cannot self-update",
                "Self-update is only supported in the bundled .exe. "
                f"Your downloaded file is at:\n{staged_path}",
            )


def _fmt_dur(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def run() -> None:
    # Ensure the log directory exists *before* basicConfig opens the file.
    config.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    from logging.handlers import RotatingFileHandler
    handler = RotatingFileHandler(
        str(config.LOG_FILE),
        maxBytes=5_000_000,
        backupCount=2,
        encoding="utf-8",
    )
    logging.basicConfig(
        level=logging.INFO,
        handlers=[handler],
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    root = tk.Tk()
    try:
        # Better default font on Windows
        from tkinter import font as tkfont
        default = tkfont.nametofont("TkDefaultFont")
        default.configure(family="Segoe UI", size=10)
    except Exception:
        pass
    app = App(root)

    # Sign-in gate. The Cloud Run backend now has GOOGLE_CLIENT_SECRET
    # configured, so the OAuth code exchange works end-to-end.
    # If IATA_GOOGLE_CLIENT_ID isn't baked (dev build / fork), we still
    # let the app run unauthenticated so a developer build keeps working.
    _REQUIRE_SIGN_IN = True
    if _REQUIRE_SIGN_IN and auth.GOOGLE_CLIENT_ID:
        if not app.ensure_signed_in():
            root.destroy()
            return
    elif not auth.GOOGLE_CLIENT_ID:
        log.info("running unauthenticated — IATA_GOOGLE_CLIENT_ID not baked")

    root.mainloop()
