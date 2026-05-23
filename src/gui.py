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

from . import (
    __version__, auth, config, excel_io, oep_client, oep_presets,
    updater, zenith_client,
)
from .bd_agency_client import Agency, fetch_all_agencies, filter_status
from .bd_cache import BDAgencyCache
from .zenith_cache import ZenithCache
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

# OEP worker → GUI message types
MSG_OEP_LOG = "oep_log"
MSG_OEP_DONE = "oep_done"         # payload: dict (see _oep_worker_run)
MSG_OEP_ERROR = "oep_error"
MSG_OEP_BUSY = "oep_busy"         # payload: str (status text)

# Zenith worker → GUI message types
MSG_ZENITH_LOG = "zenith_log"
MSG_ZENITH_PROGRESS = "zenith_progress"   # payload: (done, total, ok, nf, err)
MSG_ZENITH_RESULT = "zenith_result"        # payload: LookupResult
MSG_ZENITH_DONE = "zenith_done"            # payload: str (output path)
MSG_ZENITH_ERROR = "zenith_error"
MSG_ZENITH_LOGGED_IN = "zenith_logged_in"  # payload: dict(state_values)
MSG_ZENITH_LOGIN_FAILED = "zenith_login_failed"
# Zenith Flight Loads sub-tab
MSG_ZENITH_FL_LOG = "zenith_fl_log"
MSG_ZENITH_FL_PROGRESS = "zenith_fl_progress"  # (chunk_label, done_chunks, total_chunks, rows)
MSG_ZENITH_FL_DONE = "zenith_fl_done"           # payload: str(output path)
MSG_ZENITH_FL_ERROR = "zenith_fl_error"

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
        self.root.geometry("1080x820")
        self.root.minsize(900, 640)

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

        # ----- OEP tab state -----
        from datetime import date, timedelta
        today = date.today()
        year_ago = today - timedelta(days=365)
        self.oep_date_from = tk.StringVar(value=year_ago.isoformat())
        self.oep_date_to = tk.StringVar(value=today.isoformat())
        self.oep_mode = tk.StringVar(value="country")
        # country | division | category | gender
        self.oep_gender = tk.StringVar(value="")        # "", "1", "2", "3"
        self.oep_country_filter = tk.StringVar(value="")  # country option label
        self.oep_output_dir = tk.StringVar(value=str(Path.home() / "Documents"))
        self.oep_top_n = tk.StringVar(value="5")
        self.oep_preset_name = tk.StringVar(value="")

        self._oep_worker: threading.Thread | None = None
        self._oep_last_result: dict | None = None
        self._oep_country_options: list[oep_client.Option] = []
        # Set by the background country-list thread, drained by the main
        # thread's `_oep_poll_country_load`. Tuple of (labels, error_msg).
        self._oep_pending_country_payload: tuple[list[str], str | None] | None = None
        self._oep_preset_store = oep_presets.PresetStore(config.OEP_PRESET_FILE)
        # Embedded matplotlib chart; instantiated lazily on first time-series view.
        self._oep_chart_canvas = None
        self._oep_chart_figure = None

        # ----- Zenith tab state -----
        self.zenith_input_path = tk.StringVar()
        self.zenith_sheet_name = tk.StringVar()
        self.zenith_column_name = tk.StringVar()
        self.zenith_output_dir = tk.StringVar(value=str(Path.home() / "Documents"))
        self.zenith_username = tk.StringVar()
        self.zenith_company = tk.StringVar(value="usba")
        self.zenith_concurrency = tk.IntVar(value=3)
        self.zenith_delay_s = tk.DoubleVar(value=0.8)
        self.zenith_skip_cached = tk.BooleanVar(value=True)
        self.zenith_test_mode = tk.BooleanVar(value=False)

        self._zenith_sheet_columns: list[str] = []
        self._zenith_session: zenith_client.ZenithSession | None = None
        self._zenith_worker: threading.Thread | None = None
        self._zenith_stop_flag = threading.Event()
        self._zenith_pause_flag = threading.Event()
        self._zenith_cache = ZenithCache(config.ZENITH_CACHE_DB)

        # ----- Shared message queue -----
        self._msg_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()

        # ----- Auth + updater state -----
        self._signed_in_user: dict | None = None
        self._update_worker: threading.Thread | None = None

        self._setup_styles()
        self._build_ui()
        self._refresh_bd_status_label()
        # Show the correct auth button (Sign in or Sign out) from launch,
        # before `ensure_signed_in` runs.
        self._refresh_user_label()
        self.root.after(100, self._poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # Theme + custom widget styles
    # ------------------------------------------------------------------

    def _setup_styles(self) -> None:
        """Pick a modern theme and define our reusable styles."""
        style = ttk.Style()
        # Vista is Windows-native and looks much better than the default
        # 'default' theme. Fall through to clam on non-Windows / older Tk.
        for name in ("vista", "winnative", "clam"):
            if name in style.theme_names():
                style.theme_use(name)
                break
        # Reusable named styles
        style.configure("Section.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Hint.TLabel", foreground="#64748b", font=("Segoe UI", 9))
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"), padding=(18, 8))

    # ------------------------------------------------------------------
    # Layout helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_scrollable(parent: ttk.Frame) -> ttk.Frame:
        """Wrap `parent` in a vertical scrollable Canvas and return the inner Frame.

        Widgets added to the returned frame scroll vertically when content
        overflows the visible area. Mouse-wheel events are captured so
        users don't have to grab the scrollbar.
        """
        outer = ttk.Frame(parent)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, borderwidth=0, highlightthickness=0)
        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        inner = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_inner_configure(_event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event):
            # Make the inner frame at least as wide as the canvas viewport,
            # so child widgets that use `fill="x"` actually stretch.
            canvas.itemconfigure(window_id, width=event.width)

        inner.bind("<Configure>", _on_inner_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        # Mouse-wheel scroll while pointer is over the canvas.
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")

        def _bind_wheel(_e):
            canvas.bind_all("<MouseWheel>", _on_mousewheel)

        def _unbind_wheel(_e):
            canvas.unbind_all("<MouseWheel>")

        canvas.bind("<Enter>", _bind_wheel)
        canvas.bind("<Leave>", _unbind_wheel)
        inner.bind("<Enter>", _bind_wheel)
        inner.bind("<Leave>", _unbind_wheel)

        return inner

    @staticmethod
    def _section(parent: tk.Widget, title: str) -> ttk.Frame:
        """Section heading + separator + inner content frame.

        Returns the inner frame. Caller should `.pack(...)` widgets into it.
        """
        wrapper = ttk.Frame(parent)
        wrapper.pack(fill="x", pady=(8, 4), padx=4)
        header = ttk.Frame(wrapper)
        header.pack(fill="x")
        ttk.Label(header, text=title, style="Section.TLabel").pack(side="left")
        ttk.Separator(wrapper, orient="horizontal").pack(fill="x", pady=(2, 6))
        body = ttk.Frame(wrapper)
        body.pack(fill="x")
        return body

    @staticmethod
    def _form_row(
        parent: ttk.Frame,
        row: int,
        label: str,
        widget: tk.Widget,
        *,
        suffix: tk.Widget | None = None,
        label_width: int = 16,
    ) -> None:
        """Add a `Label: [widget]` row to a 2- or 3-column grid in `parent`."""
        ttk.Label(parent, text=label, width=label_width, anchor="w").grid(
            row=row, column=0, sticky="w", padx=(2, 8), pady=4
        )
        widget.grid(row=row, column=1, sticky="ew", padx=(0, 4), pady=4)
        if suffix is not None:
            suffix.grid(row=row, column=2, padx=(4, 2), pady=4)
        parent.columnconfigure(1, weight=1)

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

        # Two mutually-exclusive auth buttons sitting in the same slot —
        # whichever is appropriate gets packed in `_refresh_user_label`.
        self.btn_sign_out = ttk.Button(
            status_frm, text="Sign out", command=self._on_sign_out,
        )
        self.btn_sign_in = ttk.Button(
            status_frm, text="Sign in", command=self._on_sign_in,
        )

        # Wrap everything in a Notebook so each tool gets its own tab.
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        iata_tab = ttk.Frame(notebook)
        bd_tab = ttk.Frame(notebook)
        oep_tab = ttk.Frame(notebook)
        zenith_tab = ttk.Frame(notebook)
        notebook.add(iata_tab, text="IATA Code Validator")
        notebook.add(bd_tab, text="BD Travel Agency Lookup")
        notebook.add(oep_tab, text="BD Overseas Movement")
        notebook.add(zenith_tab, text="Zenith Customer Lookup")

        self._build_iata_tab(iata_tab)
        self._build_bd_tab(bd_tab)
        self._build_oep_tab(oep_tab)
        self._build_zenith_tab(zenith_tab)

    def _build_iata_tab(self, parent: ttk.Frame) -> None:
        # ----- Input -----
        body = self._section(parent, "Input Excel")

        entry = ttk.Entry(body, textvariable=self.input_path)
        self._form_row(
            body, 0, "File:",
            entry,
            suffix=ttk.Button(body, text="Browse...", command=self._pick_input),
        )

        self.sheet_combo = ttk.Combobox(body, textvariable=self.sheet_name, state="readonly")
        self.sheet_combo.bind("<<ComboboxSelected>>", lambda _e: self._reload_columns())
        self._form_row(body, 1, "Sheet:", self.sheet_combo)

        self.col_combo = ttk.Combobox(body, textvariable=self.column_name, state="readonly")
        self._form_row(body, 2, "IATA column:", self.col_combo)

        rr = ttk.Frame(body)
        ttk.Label(rr, text="from").pack(side="left")
        ttk.Entry(rr, textvariable=self.start_row, width=7).pack(side="left", padx=(4, 12))
        ttk.Label(rr, text="to").pack(side="left")
        ttk.Entry(rr, textvariable=self.end_row, width=7).pack(side="left", padx=(4, 8))
        ttk.Label(rr, text="(blank = all)", style="Hint.TLabel").pack(side="left")
        self._form_row(body, 3, "Row range:", rr)

        # ----- Output -----
        body = self._section(parent, "Output")
        entry = ttk.Entry(body, textvariable=self.output_dir)
        self._form_row(
            body, 0, "Folder:",
            entry,
            suffix=ttk.Button(body, text="Browse...", command=self._pick_output),
        )

        # ----- Controls -----
        ctrl = ttk.Frame(parent)
        ctrl.pack(fill="x", pady=(8, 4), padx=4)
        self.btn_start = ttk.Button(
            ctrl, text="Start", command=self._start, style="Primary.TButton"
        )
        self.btn_start.pack(side="left", padx=(0, 8))
        self.btn_pause = ttk.Button(ctrl, text="Pause", command=self._pause, state="disabled")
        self.btn_pause.pack(side="left", padx=4)
        self.btn_resume = ttk.Button(ctrl, text="Resume", command=self._resume, state="disabled")
        self.btn_resume.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(ctrl, text="Stop", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=4)

        # ----- Progress -----
        prog = ttk.Frame(parent)
        prog.pack(fill="x", padx=4, pady=(8, 4))
        self.progress_bar = ttk.Progressbar(prog, mode="determinate")
        self.progress_bar.pack(fill="x")
        self.progress_label = ttk.Label(prog, text="Idle.", style="Hint.TLabel")
        self.progress_label.pack(anchor="w", pady=(2, 0))

        # ----- Log -----
        body = self._section(parent, "Log")
        log_box = ttk.Frame(body)
        log_box.pack(fill="both", expand=True)
        self.log_text = tk.Text(
            log_box, height=12, wrap="none", font=("Consolas", 9),
            relief="flat", borderwidth=1, highlightthickness=1,
            highlightbackground="#cbd5e1",
        )
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(log_box, command=self.log_text.yview)
        scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scroll.set, state="disabled")

    # ------------------------------------------------------------------
    # BD Travel Agency tab
    # ------------------------------------------------------------------

    def _build_bd_tab(self, parent: ttk.Frame) -> None:
        # ----- Cached data + Refresh action -----
        body = self._section(parent, "Cached agency list  ·  regtravelagency.gov.bd")
        row = ttk.Frame(body)
        row.pack(fill="x")
        self.bd_status_label = ttk.Label(row, text="…", style="Hint.TLabel")
        self.bd_status_label.pack(side="left", padx=2)
        self.btn_bd_refresh = ttk.Button(
            row, text="↻  Refresh now", command=self._bd_refresh,
        )
        self.btn_bd_refresh.pack(side="right")

        # ----- Mode -----
        body = self._section(parent, "What to run")
        ttk.Radiobutton(
            body,
            text="Export full list to Excel (all cached agencies)",
            variable=self.bd_mode, value="full",
            command=self._toggle_bd_mode,
        ).pack(anchor="w", padx=2, pady=2)
        ttk.Radiobutton(
            body,
            text="Lookup names from Excel (match each row against the cached list)",
            variable=self.bd_mode, value="lookup",
            command=self._toggle_bd_mode,
        ).pack(anchor="w", padx=2, pady=2)

        # ----- Input (lookup mode) -----
        body = self._section(parent, "Input Excel  ·  lookup mode only")
        self.bd_input_frm = body
        self.bd_input_entry = ttk.Entry(body, textvariable=self.bd_input_path)
        self.bd_input_btn = ttk.Button(
            body, text="Browse...", command=self._bd_pick_input,
        )
        self._form_row(
            body, 0, "File:", self.bd_input_entry, suffix=self.bd_input_btn,
        )

        self.bd_sheet_combo = ttk.Combobox(
            body, textvariable=self.bd_sheet_name, state="readonly",
        )
        self.bd_sheet_combo.bind("<<ComboboxSelected>>", lambda _e: self._bd_reload_columns())
        self._form_row(body, 1, "Sheet:", self.bd_sheet_combo)

        self.bd_col_combo = ttk.Combobox(
            body, textvariable=self.bd_column_name, state="readonly",
        )
        self._form_row(body, 2, "Input column:", self.bd_col_combo)

        # Match-against checkboxes — same row, no nested frame.
        match_row = ttk.Frame(body)
        ttk.Checkbutton(
            match_row, text="Agency Name", variable=self.bd_match_name,
        ).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(
            match_row, text="License Number", variable=self.bd_match_license,
        ).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(
            match_row, text="Address", variable=self.bd_match_address,
        ).pack(side="left")
        self._form_row(body, 3, "Match against:", match_row)

        # ----- Output + filter -----
        body = self._section(parent, "Output")
        ttk.Checkbutton(
            body,
            text="Include EXPIRED-PENDING agencies (license expired but still in active list)",
            variable=self.bd_include_expired,
        ).pack(anchor="w", padx=2, pady=(0, 6))
        out_row = ttk.Frame(body)
        out_row.pack(fill="x")
        out_entry = ttk.Entry(out_row, textvariable=self.bd_output_dir)
        ttk.Label(out_row, text="Folder:", width=16, anchor="w").pack(
            side="left", padx=(2, 8)
        )
        out_entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        ttk.Button(
            out_row, text="Browse...", command=self._bd_pick_output,
        ).pack(side="right", padx=(4, 2))

        # ----- Run -----
        ctrl = ttk.Frame(parent)
        ctrl.pack(fill="x", pady=(8, 4), padx=4)
        self.btn_bd_run = ttk.Button(
            ctrl, text="Run", command=self._bd_run, style="Primary.TButton",
        )
        self.btn_bd_run.pack(side="left")

        # ----- Progress -----
        prog = ttk.Frame(parent)
        prog.pack(fill="x", padx=4, pady=(8, 4))
        self.bd_progress_bar = ttk.Progressbar(prog, mode="determinate")
        self.bd_progress_bar.pack(fill="x")
        self.bd_progress_label = ttk.Label(prog, text="Idle.", style="Hint.TLabel")
        self.bd_progress_label.pack(anchor="w", pady=(2, 0))

        # ----- Log -----
        body = self._section(parent, "Log")
        log_box = ttk.Frame(body)
        log_box.pack(fill="both", expand=True)
        self.bd_log_text = tk.Text(
            log_box, height=10, wrap="none", font=("Consolas", 9),
            relief="flat", borderwidth=1, highlightthickness=1,
            highlightbackground="#cbd5e1",
        )
        self.bd_log_text.pack(side="left", fill="both", expand=True)
        bd_scroll = ttk.Scrollbar(log_box, command=self.bd_log_text.yview)
        bd_scroll.pack(side="right", fill="y")
        self.bd_log_text.configure(yscrollcommand=bd_scroll.set, state="disabled")

        self._toggle_bd_mode()

    # ------------------------------------------------------------------
    # OEP tab (BD Overseas Movement — oep.gov.bd)
    # ------------------------------------------------------------------

    def _build_oep_tab(self, parent: ttk.Frame) -> None:
        # Wrap the whole tab so a smaller window still reaches every control.
        parent = self._make_scrollable(parent)

        # ----- Description -----
        intro = self._section(
            parent,
            "Where are Bangladeshi workers going?  ·  oep.gov.bd",
        )
        ttk.Label(
            intro,
            text=(
                "Pulls clearance data straight from the Overseas Employment "
                "Platform. Pick a date range and view, then Run."
            ),
            style="Hint.TLabel",
            wraplength=820,
            justify="left",
        ).pack(anchor="w")

        # ----- Filters -----
        body = self._section(parent, "Filters")
        date_row = ttk.Frame(body)
        ttk.Label(date_row, text="From:", width=6, anchor="w").pack(side="left")
        ttk.Entry(date_row, textvariable=self.oep_date_from, width=12).pack(
            side="left", padx=(0, 12)
        )
        ttk.Label(date_row, text="To:", width=4, anchor="w").pack(side="left")
        ttk.Entry(date_row, textvariable=self.oep_date_to, width=12).pack(
            side="left", padx=(0, 12)
        )
        ttk.Label(
            date_row,
            text="(YYYY-MM-DD — leave full range for all historical data)",
            style="Hint.TLabel",
        ).pack(side="left")
        self._form_row(body, 0, "Date range:", date_row)

        # Gender + country filter
        gender_row = ttk.Frame(body)
        for value, label in oep_client.GENDER_OPTIONS:
            ttk.Radiobutton(
                gender_row, text=label, variable=self.oep_gender, value=value,
            ).pack(side="left", padx=(0, 12))
        self._form_row(body, 1, "Gender:", gender_row)

        self.oep_country_combo = ttk.Combobox(
            body, textvariable=self.oep_country_filter, state="readonly",
            values=("(loading…)",),
        )
        self._form_row(body, 2, "Country filter:", self.oep_country_combo)

        # Multi-country selector (used by time-series + pivot only)
        multi_row = ttk.Frame(body)
        self.oep_country_listbox = tk.Listbox(
            multi_row, selectmode="extended", height=5, exportselection=False,
        )
        self.oep_country_listbox.pack(side="left", fill="x", expand=True)
        multi_scroll = ttk.Scrollbar(multi_row, command=self.oep_country_listbox.yview)
        multi_scroll.pack(side="left", fill="y")
        self.oep_country_listbox.configure(yscrollcommand=multi_scroll.set)
        multi_btns = ttk.Frame(multi_row)
        multi_btns.pack(side="left", padx=(8, 0))
        for n in (5, 10, 20):
            ttk.Button(
                multi_btns, text=f"Top {n}",
                command=lambda n=n: self._oep_set_top_n(n),
                width=8,
            ).pack(anchor="w", pady=1)
        ttk.Button(
            multi_btns, text="All",
            command=self._oep_select_all,
            width=8,
        ).pack(anchor="w", pady=1)
        ttk.Button(
            multi_btns, text="Clear",
            command=lambda: self.oep_country_listbox.selection_clear(0, "end"),
            width=8,
        ).pack(anchor="w", pady=1)
        self._form_row(body, 3, "Countries (multi):", multi_row)

        # ----- Presets -----
        body = self._section(parent, "Saved filter presets")
        preset_row = ttk.Frame(body)
        preset_row.pack(fill="x")
        ttk.Label(preset_row, text="Preset:", width=8, anchor="w").pack(side="left")
        self.oep_preset_combo = ttk.Combobox(
            preset_row, textvariable=self.oep_preset_name, state="readonly", width=30,
        )
        self.oep_preset_combo.pack(side="left", padx=(0, 8))
        ttk.Button(preset_row, text="Load", command=self._oep_preset_load).pack(
            side="left", padx=(0, 4)
        )
        ttk.Button(preset_row, text="Save current as...", command=self._oep_preset_save).pack(
            side="left", padx=(0, 4)
        )
        ttk.Button(preset_row, text="Delete", command=self._oep_preset_delete).pack(
            side="left"
        )
        self._oep_refresh_preset_list()

        # ----- View mode -----
        body = self._section(parent, "View")
        for value, label in (
            ("country", "Top destination countries"),
            ("division", "Top source districts (Bangladesh)"),
            ("category", "Top job categories"),
            ("gender", "Gender breakdown per destination"),
            ("timeseries", "Monthly time series (chart)  ·  needs multi-country selection"),
            ("pivot", "Country × Division pivot  ·  needs multi-country selection"),
            ("full", "Full report — every view combined into one Excel  ·  needs multi-country"),
            ("cdt", "Country × Division × Month — single flat sheet  ·  HEAVY, needs multi-country"),
            ("cdtd", "Country × District × Month — district granularity  ·  HEAVY, needs multi-country"),
        ):
            ttk.Radiobutton(
                body, text=label, variable=self.oep_mode, value=value,
            ).pack(anchor="w", padx=2, pady=1)

        # ----- Run / Export -----
        ctrl = ttk.Frame(parent)
        ctrl.pack(fill="x", pady=(8, 4), padx=4)
        self.btn_oep_run = ttk.Button(
            ctrl, text="Run", command=self._oep_run, style="Primary.TButton",
        )
        self.btn_oep_run.pack(side="left")
        self.btn_oep_export = ttk.Button(
            ctrl, text="Export to Excel...", command=self._oep_export, state="disabled",
        )
        self.btn_oep_export.pack(side="left", padx=(8, 0))

        self.oep_status_label = ttk.Label(ctrl, text="Idle.", style="Hint.TLabel")
        self.oep_status_label.pack(side="left", padx=12)

        # ----- Results panel — tree OR chart, depending on mode -----
        body = self._section(parent, "Results")
        # `oep_results_holder` holds whichever widget is currently visible
        # (tree by default, chart for time-series). Swapping uses pack/forget.
        self.oep_results_holder = ttk.Frame(body)
        self.oep_results_holder.pack(fill="both", expand=True)

        self.oep_tree_frame = ttk.Frame(self.oep_results_holder)
        self.oep_tree = ttk.Treeview(self.oep_tree_frame, show="headings", height=14)
        self.oep_tree.pack(side="left", fill="both", expand=True)
        tree_scroll = ttk.Scrollbar(self.oep_tree_frame, command=self.oep_tree.yview)
        tree_scroll.pack(side="right", fill="y")
        self.oep_tree.configure(yscrollcommand=tree_scroll.set)
        # Double-click on a country row → category drilldown popup.
        self.oep_tree.bind("<Double-1>", self._oep_on_double_click)

        # Default columns; replaced when results arrive.
        self._oep_set_columns([("#", 40), ("Info", 600)])
        self.oep_tree.insert(
            "", "end",
            values=("—", "Click Run to fetch data. Double-click a country to drill into job categories."),
        )

        # Chart frame is created lazily the first time time-series view runs.
        self.oep_chart_frame = ttk.Frame(self.oep_results_holder)

        self._oep_show_tree()

        # Load country list async so the form is usable immediately.
        # The thread fetches; the main loop polls — see _oep_poll_country_load.
        threading.Thread(target=self._oep_load_countries, daemon=True).start()
        self.root.after(200, self._oep_poll_country_load)

    # --- helpers for the multi-select listbox + presets ---

    def _oep_set_top_n(self, n: int) -> None:
        """Select the first `n` entries in the country listbox."""
        if not self.oep_country_listbox.size():
            messagebox.showinfo("OEP", "Country list still loading — try again in a moment.")
            return
        self.oep_country_listbox.selection_clear(0, "end")
        for i in range(min(n, self.oep_country_listbox.size())):
            self.oep_country_listbox.selection_set(i)
        self.oep_country_listbox.see(0)

    def _oep_select_all(self) -> None:
        """Select every country in the listbox.

        Time-series still does one call per month (cheap regardless of
        country count); pivot does one call per country, so 200 countries
        means ~200 round-trips. The progress bar handles the wait.
        """
        size = self.oep_country_listbox.size()
        if not size:
            messagebox.showinfo("OEP", "Country list still loading — try again in a moment.")
            return
        self.oep_country_listbox.selection_set(0, "end")

    def _oep_selected_countries(self) -> list[oep_client.Option]:
        """Return the currently selected items as Option objects."""
        out: list[oep_client.Option] = []
        for idx in self.oep_country_listbox.curselection():
            label = self.oep_country_listbox.get(idx)
            for opt in self._oep_country_options:
                if opt.label == label:
                    out.append(opt)
                    break
        return out

    def _oep_apply_country_selection(self, country_ids: list[str]) -> None:
        """Highlight matching country IDs in the listbox (used by preset load)."""
        self.oep_country_listbox.selection_clear(0, "end")
        for i in range(self.oep_country_listbox.size()):
            label = self.oep_country_listbox.get(i)
            opt = next((o for o in self._oep_country_options if o.label == label), None)
            if opt and opt.value in country_ids:
                self.oep_country_listbox.selection_set(i)

    # --- chart/tree panel swap ---

    def _oep_show_tree(self) -> None:
        try:
            self.oep_chart_frame.pack_forget()
        except Exception:
            pass
        self.oep_tree_frame.pack(fill="both", expand=True)

    def _oep_show_chart(self) -> None:
        try:
            self.oep_tree_frame.pack_forget()
        except Exception:
            pass
        self.oep_chart_frame.pack(fill="both", expand=True)

    # --- preset bar ---

    def _oep_refresh_preset_list(self) -> None:
        try:
            names = self._oep_preset_store.list_names()
        except Exception as exc:  # noqa: BLE001
            log.warning("Preset list failed: %s", exc)
            names = []
        self.oep_preset_combo.configure(values=names)

    def _oep_preset_load(self) -> None:
        name = self.oep_preset_name.get().strip()
        if not name:
            messagebox.showinfo("OEP", "Pick a preset from the dropdown first.")
            return
        preset = self._oep_preset_store.get(name)
        if not preset:
            messagebox.showerror("OEP", f"Preset {name!r} not found.")
            return
        self.oep_date_from.set(preset.date_from)
        self.oep_date_to.set(preset.date_to)
        self.oep_mode.set(preset.mode)
        self.oep_gender.set(preset.gender_id or "")
        if preset.country_ids:
            self._oep_apply_country_selection(preset.country_ids)

    def _oep_preset_save(self) -> None:
        from tkinter import simpledialog
        default = self.oep_preset_name.get() or self.oep_mode.get().title()
        name = simpledialog.askstring(
            "Save preset",
            "Preset name:",
            initialvalue=default,
            parent=self.root,
        )
        if not name:
            return
        name = name.strip()
        if not name:
            return
        selected = self._oep_selected_countries()
        preset = oep_presets.OEPPreset(
            name=name,
            mode=self.oep_mode.get(),
            date_from=self.oep_date_from.get(),
            date_to=self.oep_date_to.get(),
            gender_id=self.oep_gender.get(),
            country_ids=[o.value for o in selected],
            country_labels=[o.label for o in selected],
        )
        try:
            self._oep_preset_store.save(preset)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("OEP", f"Could not save preset:\n{exc}")
            return
        self._oep_refresh_preset_list()
        self.oep_preset_name.set(name)
        messagebox.showinfo("OEP", f"Preset {name!r} saved.")

    def _oep_preset_delete(self) -> None:
        name = self.oep_preset_name.get().strip()
        if not name:
            return
        if not messagebox.askyesno("OEP", f"Delete preset {name!r}?"):
            return
        removed = self._oep_preset_store.delete(name)
        if not removed:
            messagebox.showinfo("OEP", "That preset doesn't exist anymore.")
        self._oep_refresh_preset_list()
        self.oep_preset_name.set("")

    # --- category drilldown popup ---

    def _oep_on_double_click(self, _event) -> None:
        """If we're in 'country' mode, open a category drilldown for the row."""
        if not self._oep_last_result:
            return
        if self._oep_last_result.get("mode") != "country":
            return
        sel = self.oep_tree.selection()
        if not sel:
            return
        values = self.oep_tree.item(sel[0], "values")
        if not values or len(values) < 2:
            return
        country_name = values[1]
        rows = self._oep_last_result.get("raw") or []
        cats = oep_client.categories_for_country(rows, country_name)
        if not cats:
            messagebox.showinfo("OEP", f"No category data for {country_name}.")
            return
        self._oep_open_category_popup(country_name, cats)

    def _oep_open_category_popup(self, country_name: str, cats: list) -> None:
        win = tk.Toplevel(self.root)
        win.title(f"Job categories — {country_name}")
        win.geometry("560x500")
        win.transient(self.root)

        header = ttk.Label(
            win,
            text=f"Top jobs for {country_name}  ·  {sum(c.total_employee for c in cats):,} total workers",
            style="Section.TLabel",
        )
        header.pack(anchor="w", padx=10, pady=(10, 4))

        tree_frame = ttk.Frame(win)
        tree_frame.pack(fill="both", expand=True, padx=10, pady=10)
        tree = ttk.Treeview(
            tree_frame, show="headings",
            columns=("Rank", "Job Category", "Total", "Share %"),
        )
        tree.heading("Rank", text="Rank")
        tree.heading("Job Category", text="Job Category")
        tree.heading("Total", text="Total")
        tree.heading("Share %", text="Share %")
        tree.column("Rank", width=50, anchor="e")
        tree.column("Job Category", width=300, anchor="w")
        tree.column("Total", width=100, anchor="e")
        tree.column("Share %", width=80, anchor="e")
        tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(tree_frame, command=tree.yview)
        sb.pack(side="right", fill="y")
        tree.configure(yscrollcommand=sb.set)

        grand = sum(c.total_employee for c in cats) or 1
        for rank, c in enumerate(cats, start=1):
            tree.insert("", "end", values=(
                rank, c.category_name, f"{c.total_employee:,}",
                f"{100 * c.total_employee / grand:.2f}",
            ))

        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 10))

    def _oep_set_columns(self, cols: list[tuple[str, int]]) -> None:
        """Reset the Treeview columns + headings."""
        ids = [c[0] for c in cols]
        self.oep_tree.configure(columns=ids)
        for col_id, width in cols:
            self.oep_tree.heading(col_id, text=col_id)
            anchor = "e" if any(t in col_id.lower() for t in ("total", "%", "count", "male", "female", "other", "rank", "#")) else "w"
            self.oep_tree.column(col_id, width=width, anchor=anchor, stretch=True)

    def _oep_load_countries(self) -> None:
        """Fetch country list off-thread and stash the result.

        We deliberately *don't* call `root.after` from this thread —
        when the sign-in modal is active during app startup, the
        scheduled callback sometimes never fires. Instead the main
        thread polls `_oep_pending_country_payload` via
        `_oep_poll_country_load`, which is rock-solid because it runs
        on the Tk event loop itself.
        """
        log.info("OEP loading country list from oep.gov.bd…")
        error_msg: str | None = None
        try:
            opts = oep_client.list_countries()
            log.info("OEP loaded %d country options", len(opts))
        except Exception as exc:  # noqa: BLE001 — show fallback in UI
            log.warning("OEP countries fetch failed: %s", exc)
            opts = []
            error_msg = f"{type(exc).__name__}: {exc}"
        self._oep_country_options = opts
        labels = ["(All countries)"] + [o.label for o in opts]
        # Set the pending payload — the main-thread poller will pick it up.
        self._oep_pending_country_payload = (labels, error_msg)

    def _oep_poll_country_load(self) -> None:
        """Run on the Tk main loop; applies country options once available."""
        payload = self._oep_pending_country_payload
        if payload is not None:
            self._oep_pending_country_payload = None
            labels, error_msg = payload
            try:
                self._oep_apply_country_options(labels, error_msg)
            except Exception:  # noqa: BLE001 — never let a UI hiccup kill the poll
                log.exception("OEP country-apply failed")
            return
        # Still waiting — keep polling. 200ms is fast enough that the user
        # never notices, slow enough to be free CPU-wise.
        try:
            self.root.after(200, self._oep_poll_country_load)
        except RuntimeError:
            return

    def _oep_apply_country_options(
        self, labels: list[str], error_msg: str | None = None,
    ) -> None:
        log.info("OEP applying %d country options to UI", len(self._oep_country_options))
        self.oep_country_combo.configure(values=labels)
        if self.oep_country_filter.get() in ("", "(loading…)"):
            self.oep_country_filter.set("(All countries)")
        # The listbox holds only the real options — no "(All countries)" entry.
        self.oep_country_listbox.delete(0, "end")
        for o in self._oep_country_options:
            self.oep_country_listbox.insert("end", o.label)
        if error_msg:
            self.oep_status_label.configure(
                text=f"⚠ Could not load country list: {error_msg}",
            )
        elif not self._oep_country_options:
            self.oep_status_label.configure(
                text="⚠ Country list returned zero entries — oep.gov.bd may be down",
            )
        else:
            self.oep_status_label.configure(
                text=f"Ready · {len(self._oep_country_options)} countries loaded",
            )

    def _oep_resolve_country_id(self) -> str:
        label = self.oep_country_filter.get().strip()
        if not label or label.startswith("(All"):
            return ""
        for opt in self._oep_country_options:
            if opt.label == label:
                return opt.value
        return ""

    def _oep_run(self) -> None:
        if self._oep_worker is not None and self._oep_worker.is_alive():
            messagebox.showinfo("OEP", "A request is already running.")
            return
        date_from = self.oep_date_from.get().strip()
        date_to = self.oep_date_to.get().strip()
        try:
            oep_client._validate_date(date_from, "date_from")
            oep_client._validate_date(date_to, "date_to")
        except ValueError as exc:
            messagebox.showerror("OEP", str(exc))
            return
        if date_from > date_to:
            messagebox.showerror("OEP", "'From' date must be on or before 'To' date.")
            return

        mode = self.oep_mode.get()
        gender = self.oep_gender.get() or ""
        country_id = self._oep_resolve_country_id()
        selected_multi = self._oep_selected_countries()
        if mode in ("timeseries", "pivot", "full", "cdt", "cdtd") and not selected_multi:
            messagebox.showerror(
                "OEP",
                f"The {mode} view needs at least one country selected in the "
                "multi-select box. Click 'Top 5' or 'All' to start.",
            )
            return
        if mode in ("cdt", "cdtd", "full"):
            n_months = len(oep_client.iter_year_months(date_from, date_to))
            # Full report caps the heavy sections to 15 countries internally
            # — see `_oep_worker_run`. Standalone CDT honours the full selection.
            FULL_REPORT_HEAVY_CAP = 15
            effective_countries = (
                min(len(selected_multi), FULL_REPORT_HEAVY_CAP)
                if mode == "full" else len(selected_multi)
            )
            est_calls = effective_countries * n_months
            est_minutes = max(1, int(est_calls * 5 / 60))
            if est_calls > 50:
                if mode == "cdt":
                    section_label = "The country×division×month sheet"
                    cap_note = ""
                elif mode == "cdtd":
                    section_label = "The country×district×month sheet"
                    cap_note = ""
                else:
                    section_label = "The Full report's heavy sections (pivot, time-series, country×division×month)"
                    cap_note = (
                        f"\n\nNote: Full report auto-caps to the top {FULL_REPORT_HEAVY_CAP} "
                        f"destinations by volume (you selected {len(selected_multi)})."
                    )
                proceed = messagebox.askyesno(
                    "OEP — heavy fetch",
                    f"{section_label} needs "
                    f"{effective_countries} countries × {n_months} months = "
                    f"{est_calls} HTTP calls (~{est_minutes} min).{cap_note}\n\n"
                    "Continue?",
                )
                if not proceed:
                    return
        cfg = {
            "mode": mode,
            "date_from": date_from,
            "date_to": date_to,
            "gender_id": gender,
            "country_id": country_id,
            "country_options": selected_multi,
        }
        self._oep_last_result = None
        self.btn_oep_run.configure(state="disabled")
        self.btn_oep_export.configure(state="disabled")
        self._oep_show_tree()
        self.oep_status_label.configure(text="Fetching from oep.gov.bd…")
        self.oep_tree.delete(*self.oep_tree.get_children())
        self._oep_set_columns([("#", 40), ("Info", 600)])
        self.oep_tree.insert("", "end", values=("…", "Working — this may take 30-60s for wide date ranges."))

        self._oep_worker = threading.Thread(
            target=self._oep_worker_run, args=(cfg,), daemon=True,
        )
        self._oep_worker.start()

    def _oep_worker_run(self, cfg: dict) -> None:
        mode = cfg["mode"]
        try:
            if mode == "country":
                rows = oep_client.fetch_country_clearance(
                    cfg["date_from"], cfg["date_to"],
                    gender_id=cfg["gender_id"],
                    country_id=cfg["country_id"],
                )
                summary = oep_client.aggregate_by_country(rows)
                self._post(MSG_OEP_DONE, {
                    "mode": "country",
                    "summary": summary,
                    "raw": rows,
                    "date_from": cfg["date_from"],
                    "date_to": cfg["date_to"],
                })
            elif mode == "category":
                rows = oep_client.fetch_country_clearance(
                    cfg["date_from"], cfg["date_to"],
                    gender_id=cfg["gender_id"],
                    country_id=cfg["country_id"],
                )
                summary = oep_client.aggregate_by_category(rows)
                self._post(MSG_OEP_DONE, {
                    "mode": "category",
                    "summary": summary,
                    "raw": rows,
                    "date_from": cfg["date_from"],
                    "date_to": cfg["date_to"],
                })
            elif mode == "division":
                country_ids = [cfg["country_id"]] if cfg["country_id"] else None
                rows = oep_client.fetch_division_clearance(
                    cfg["date_from"], cfg["date_to"],
                    gender_id=cfg["gender_id"],
                    country_ids=country_ids,
                )
                div_summary = oep_client.aggregate_by_division(rows)
                self._post(MSG_OEP_DONE, {
                    "mode": "division",
                    "summary": div_summary,
                    "raw": rows,
                    "date_from": cfg["date_from"],
                    "date_to": cfg["date_to"],
                })
            elif mode == "gender":
                self._post(MSG_OEP_BUSY, "Fetching All…")
                all_rows = oep_client.fetch_country_clearance(
                    cfg["date_from"], cfg["date_to"],
                    country_id=cfg["country_id"],
                )
                self._post(MSG_OEP_BUSY, "Fetching Male…")
                male_rows = oep_client.fetch_country_clearance(
                    cfg["date_from"], cfg["date_to"],
                    gender_id="1", country_id=cfg["country_id"],
                )
                self._post(MSG_OEP_BUSY, "Fetching Female…")
                female_rows = oep_client.fetch_country_clearance(
                    cfg["date_from"], cfg["date_to"],
                    gender_id="2", country_id=cfg["country_id"],
                )
                summary = oep_client.merge_gender_breakdowns(
                    all_rows, male_rows, female_rows,
                )
                self._post(MSG_OEP_DONE, {
                    "mode": "gender",
                    "summary": summary,
                    "raw": all_rows,
                    "date_from": cfg["date_from"],
                    "date_to": cfg["date_to"],
                })
            elif mode == "timeseries":
                opts = cfg["country_options"]

                def progress(idx: int, total: int, label: str) -> None:
                    self._post(MSG_OEP_BUSY, f"{label}  ({idx}/{total})")

                rows = oep_client.fetch_monthly_timeseries(
                    cfg["date_from"], cfg["date_to"],
                    country_ids=[o.value for o in opts],
                    gender_id=cfg["gender_id"],
                    progress_cb=progress,
                )
                months, series = oep_client.pivot_timeseries(rows)
                self._post(MSG_OEP_DONE, {
                    "mode": "timeseries",
                    "months": months,
                    "series": series,
                    "raw": rows,
                    "country_labels": [o.label for o in opts],
                    "date_from": cfg["date_from"],
                    "date_to": cfg["date_to"],
                })
            elif mode == "pivot":
                opts = cfg["country_options"]

                def progress(idx: int, total: int, label: str) -> None:
                    self._post(MSG_OEP_BUSY, f"{label}  ({idx}/{total})")

                cells = oep_client.fetch_country_division_pivot(
                    cfg["date_from"], cfg["date_to"],
                    country_options=opts,
                    gender_id=cfg["gender_id"],
                    progress_cb=progress,
                )
                divisions, countries, table = oep_client.pivot_country_division(cells)
                self._post(MSG_OEP_DONE, {
                    "mode": "pivot",
                    "divisions": divisions,
                    "countries": countries,
                    "table": table,
                    "raw": cells,
                    "date_from": cfg["date_from"],
                    "date_to": cfg["date_to"],
                })
            elif mode == "full":
                user_opts = cfg["country_options"]

                def progress(idx: int, total: int, label: str) -> None:
                    self._post(MSG_OEP_BUSY, f"{label}  ({idx}/{total})")

                # 1) all-rows (no country filter) — powers country/category/gender views
                self._post(MSG_OEP_BUSY, "[1/7] All-country totals…")
                all_rows = oep_client.fetch_country_clearance(
                    cfg["date_from"], cfg["date_to"],
                    gender_id=cfg["gender_id"],
                )
                # 2) male-only and female-only — for gender breakdown
                self._post(MSG_OEP_BUSY, "[2/7] Male totals…")
                male_rows = oep_client.fetch_country_clearance(
                    cfg["date_from"], cfg["date_to"], gender_id="1",
                )
                self._post(MSG_OEP_BUSY, "[3/7] Female totals…")
                female_rows = oep_client.fetch_country_clearance(
                    cfg["date_from"], cfg["date_to"], gender_id="2",
                )
                # 4) division totals (Bangladesh-side)
                self._post(MSG_OEP_BUSY, "[4/7] Division totals…")
                div_rows = oep_client.fetch_division_clearance(
                    cfg["date_from"], cfg["date_to"], gender_id=cfg["gender_id"],
                )
                # Cap the heavy per-country sections (time-series, pivot, CDT)
                # to the top destinations by volume. Iterating 200 countries
                # gets us IP-rate-limited on oep.gov.bd, and a 200-column
                # pivot is unreadable anyway. The user's multi-selection is
                # used only as an upper bound — if they picked Top 5 we
                # honour that, but if they picked All we silently cap to 15.
                FULL_REPORT_HEAVY_CAP = 15
                country_summary = oep_client.aggregate_by_country(all_rows)
                top_country_names = [
                    c.country_name for c in country_summary[:FULL_REPORT_HEAVY_CAP]
                ]
                # Limit to the intersection of user's selection × top-N by
                # volume, preserving the country-options shape for downstream
                # calls.
                user_names = {o.label for o in user_opts}
                heavy_opts = [
                    o for o in user_opts
                    if o.label in top_country_names
                ]
                if not heavy_opts:
                    # User picked countries with zero volume in this window —
                    # fall back to top-N from the data so the report isn't empty.
                    label_to_opt = {o.label: o for o in user_opts}
                    heavy_opts = [
                        label_to_opt[name] for name in top_country_names
                        if name in label_to_opt
                    ][:FULL_REPORT_HEAVY_CAP]
                # Order heavy_opts by volume descending for predictable charts.
                rank = {name: i for i, name in enumerate(top_country_names)}
                heavy_opts = sorted(heavy_opts, key=lambda o: rank.get(o.label, 999))
                log.info(
                    "Full report heavy sections: user picked %d countries, "
                    "capped to %d for pivot/CDT/time-series",
                    len(user_opts), len(heavy_opts),
                )

                # 5) monthly time series for the heavy-capped country set
                self._post(MSG_OEP_BUSY, "[5/7] Monthly time series…")
                ts_rows = oep_client.fetch_monthly_timeseries(
                    cfg["date_from"], cfg["date_to"],
                    country_ids=[o.value for o in heavy_opts],
                    gender_id=cfg["gender_id"],
                    progress_cb=progress,
                )
                months, series = oep_client.pivot_timeseries(ts_rows)
                # 6) country × division pivot for the heavy-capped country set
                self._post(MSG_OEP_BUSY, "[6/7] Country × Division pivot…")
                cells = oep_client.fetch_country_division_pivot(
                    cfg["date_from"], cfg["date_to"],
                    country_options=heavy_opts,
                    gender_id=cfg["gender_id"],
                    progress_cb=progress,
                )
                divisions, pivot_countries, table = oep_client.pivot_country_division(cells)
                # 7) heavy: country × division × month flat sheet
                self._post(MSG_OEP_BUSY, "[7/7] Country × Division × Month…")
                cdt_cells = oep_client.fetch_country_division_timeseries(
                    cfg["date_from"], cfg["date_to"],
                    country_options=heavy_opts,
                    gender_id=cfg["gender_id"],
                    progress_cb=progress,
                )
                cdt_months, cdt_pairs, cdt_table = oep_client.pivot_country_division_timeseries(
                    cdt_cells,
                )

                self._post(MSG_OEP_DONE, {
                    "mode": "full",
                    "country_summary": country_summary,
                    "category_summary": oep_client.aggregate_by_category(all_rows),
                    "division_summary": oep_client.aggregate_by_division(div_rows),
                    "gender_summary": oep_client.merge_gender_breakdowns(
                        all_rows, male_rows, female_rows,
                    ),
                    "raw_country": all_rows,
                    "raw_division": div_rows,
                    "months": months,
                    "series": series,
                    "divisions": divisions,
                    "pivot_countries": pivot_countries,
                    "table": table,
                    # Country × Division × Month (flat sheet) — same shape as
                    # the standalone 'cdt' view but bundled here too.
                    "cdt_months": cdt_months,
                    "cdt_pairs": cdt_pairs,
                    "cdt_table": cdt_table,
                    "country_labels": [o.label for o in heavy_opts],
                    "user_country_labels": [o.label for o in user_opts],
                    "heavy_country_cap": FULL_REPORT_HEAVY_CAP,
                    "date_from": cfg["date_from"],
                    "date_to": cfg["date_to"],
                    "gender_id": cfg["gender_id"],
                })
            elif mode == "cdt":
                opts = cfg["country_options"]

                def progress(idx: int, total: int, label: str) -> None:
                    self._post(MSG_OEP_BUSY, f"{label}  ({idx}/{total})")

                cells = oep_client.fetch_country_division_timeseries(
                    cfg["date_from"], cfg["date_to"],
                    country_options=opts,
                    gender_id=cfg["gender_id"],
                    progress_cb=progress,
                )
                months, pairs, table = oep_client.pivot_country_division_timeseries(cells)
                self._post(MSG_OEP_DONE, {
                    "mode": "cdt",
                    "months": months,
                    "pairs": pairs,
                    "table": table,
                    "raw": cells,
                    "country_labels": [o.label for o in opts],
                    "date_from": cfg["date_from"],
                    "date_to": cfg["date_to"],
                })
            elif mode == "cdtd":
                opts = cfg["country_options"]

                def progress(idx: int, total: int, label: str) -> None:
                    self._post(MSG_OEP_BUSY, f"{label}  ({idx}/{total})")

                cells = oep_client.fetch_country_district_timeseries(
                    cfg["date_from"], cfg["date_to"],
                    country_options=opts,
                    gender_id=cfg["gender_id"],
                    progress_cb=progress,
                )
                months, triples, table = oep_client.pivot_country_district_timeseries(cells)
                self._post(MSG_OEP_DONE, {
                    "mode": "cdtd",
                    "months": months,
                    "triples": triples,
                    "table": table,
                    "raw": cells,
                    "country_labels": [o.label for o in opts],
                    "date_from": cfg["date_from"],
                    "date_to": cfg["date_to"],
                })
            else:
                self._post(MSG_OEP_ERROR, f"Unknown mode: {mode}")
        except Exception as exc:  # noqa: BLE001 — surface in UI
            log.exception("OEP worker failed")
            self._post(MSG_OEP_ERROR, f"{type(exc).__name__}: {exc}")

    def _oep_render_results(self, result: dict) -> None:
        self._oep_last_result = result
        mode = result["mode"]
        # Modes with a flat "summary" list (country/division/category/gender)
        # share a rendering branch below. Timeseries + pivot carry richer
        # payloads instead.
        summary = result.get("summary") or []
        self.oep_tree.delete(*self.oep_tree.get_children())

        if mode == "country":
            cols = [("Rank", 50), ("Destination", 240), ("Total", 110), ("Categories", 100), ("Share %", 90)]
            self._oep_set_columns(cols)
            grand = sum(c.total_employee for c in summary) or 1
            for rank, c in enumerate(summary, start=1):
                self.oep_tree.insert("", "end", values=(
                    rank, c.country_name, f"{c.total_employee:,}",
                    c.category_count, f"{100 * c.total_employee / grand:.2f}",
                ))
            self.oep_status_label.configure(
                text=f"{len(summary):,} destinations  ·  {grand:,} total workers"
            )
        elif mode == "division":
            cols = [("Rank", 50), ("Division", 200), ("Total", 110), ("Districts", 90), ("Share %", 90)]
            self._oep_set_columns(cols)
            grand = sum(d.total_employee for d in summary) or 1
            for rank, d in enumerate(summary, start=1):
                self.oep_tree.insert("", "end", values=(
                    rank, d.division, f"{d.total_employee:,}",
                    d.district_count, f"{100 * d.total_employee / grand:.2f}",
                ))
            self.oep_status_label.configure(
                text=f"{len(summary)} divisions  ·  {len(result['raw'])} districts  ·  {grand:,} total"
            )
        elif mode == "category":
            cols = [("Rank", 50), ("Job Category", 260), ("Total", 110), ("Countries", 90), ("Share %", 90)]
            self._oep_set_columns(cols)
            grand = sum(c.total_employee for c in summary) or 1
            for rank, c in enumerate(summary, start=1):
                self.oep_tree.insert("", "end", values=(
                    rank, c.category_name, f"{c.total_employee:,}",
                    c.country_count, f"{100 * c.total_employee / grand:.2f}",
                ))
            self.oep_status_label.configure(
                text=f"{len(summary):,} job categories  ·  {grand:,} total workers"
            )
        elif mode == "gender":
            cols = [
                ("Rank", 50), ("Destination", 220),
                ("Male", 90), ("Female", 90), ("Other", 70),
                ("Total", 100), ("Female %", 80),
            ]
            self._oep_set_columns(cols)
            for rank, g in enumerate(summary, start=1):
                pct = (100 * g.female / g.total) if g.total else 0.0
                self.oep_tree.insert("", "end", values=(
                    rank, g.country_name,
                    f"{g.male:,}", f"{g.female:,}", f"{g.other:,}",
                    f"{g.total:,}", f"{pct:.1f}",
                ))
            self.oep_status_label.configure(
                text=f"{len(summary):,} destinations  ·  gender breakdown"
            )
        elif mode == "timeseries":
            months = result["months"]
            series = result["series"]
            self._oep_render_timeseries(months, series, result)
        elif mode == "pivot":
            self._oep_render_pivot(result)
        elif mode == "full":
            self._oep_render_full_report(result)
        elif mode == "cdt":
            self._oep_render_cdt(result)
        elif mode == "cdtd":
            self._oep_render_cdtd(result)

        self.btn_oep_run.configure(state="normal")
        has_data = bool(
            summary if mode in ("country", "division", "category", "gender") else
            (result.get("series") if mode == "timeseries" else result.get("table"))
        )
        if has_data:
            self.btn_oep_export.configure(state="normal")

    def _oep_render_timeseries(
        self, months: list[str], series: dict[str, list[int]], result: dict
    ) -> None:
        # Lazy import so non-OEP users don't pay the matplotlib startup cost.
        import matplotlib
        matplotlib.use("TkAgg", force=False)
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

        if self._oep_chart_canvas is not None:
            self._oep_chart_canvas.get_tk_widget().destroy()
            self._oep_chart_canvas = None

        fig = Figure(figsize=(8, 4.5), dpi=100)
        ax = fig.add_subplot(111)
        # Preserve user-selected order for legend stacking.
        order = [c for c in result.get("country_labels", []) if c in series]
        order += [c for c in series if c not in order]
        for country in order:
            values = series[country]
            ax.plot(months, values, marker="o", label=country, linewidth=1.6)
        ax.set_title(
            f"Monthly clearance volume  ·  {result['date_from']} → {result['date_to']}"
        )
        ax.set_ylabel("Workers cleared")
        ax.set_xlabel("Month")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="best", fontsize=8)
        # Rotate ticks so YYYY-MM labels don't collide.
        for tick in ax.get_xticklabels():
            tick.set_rotation(45)
            tick.set_ha("right")
        fig.tight_layout()

        canvas = FigureCanvasTkAgg(fig, master=self.oep_chart_frame)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True)
        self._oep_chart_canvas = canvas
        self._oep_chart_figure = fig

        self._oep_show_chart()
        total = sum(sum(v) for v in series.values())
        self.oep_status_label.configure(
            text=(
                f"{len(series)} countries  ·  {len(months)} months  ·  "
                f"{total:,} total workers"
            )
        )

    def _oep_render_cdtd(self, result: dict) -> None:
        """Render the flat country×district×month table.

        Same shape as the division-level view but each row carries a
        Division + District pair. Many more rows; same column count.
        """
        months: list[str] = result["months"]
        triples: list[tuple[str, str, str]] = result["triples"]
        table: dict[tuple[str, str, str, str], int] = result["table"]

        cols = (
            [("Country", 140), ("Division", 110), ("District", 130)]
            + [(ym, 70) for ym in months]
            + [("Total", 90)]
        )
        self._oep_set_columns(cols)

        col_totals = [0] * len(months)
        grand_total = 0
        for country, division, district in triples:
            row_values = [country, division, district]
            row_total = 0
            for ci, ym in enumerate(months):
                v = table.get((country, division, district, ym), 0)
                row_total += v
                col_totals[ci] += v
                row_values.append(f"{v:,}" if v else "—")
            row_values.append(f"{row_total:,}")
            grand_total += row_total
            self.oep_tree.insert("", "end", values=row_values)

        totals_row = (
            ["Total", "", ""]
            + [f"{t:,}" for t in col_totals]
            + [f"{grand_total:,}"]
        )
        self.oep_tree.insert("", "end", values=totals_row, tags=("total",))
        self.oep_tree.tag_configure("total", font=("Segoe UI", 9, "bold"))

        self.oep_status_label.configure(
            text=(
                f"{len(triples)} (country, division, district) rows × "
                f"{len(months)} months  ·  {grand_total:,} workers"
            )
        )

    def _oep_render_cdt(self, result: dict) -> None:
        """Render the flat country×division×month table.

        With many countries and months this is wide — the on-screen view
        is the same shape as the exported Excel, so users can spot-check
        before saving.
        """
        months: list[str] = result["months"]
        pairs: list[tuple[str, str]] = result["pairs"]
        table: dict[tuple[str, str, str], int] = result["table"]

        cols = (
            [("Country", 160), ("Division", 130)]
            + [(ym, 70) for ym in months]
            + [("Total", 90)]
        )
        self._oep_set_columns(cols)

        col_totals = [0] * len(months)
        grand_total = 0
        for country, division in pairs:
            row_values = [country, division]
            row_total = 0
            for ci, ym in enumerate(months):
                v = table.get((country, division, ym), 0)
                row_total += v
                col_totals[ci] += v
                row_values.append(f"{v:,}" if v else "—")
            row_values.append(f"{row_total:,}")
            grand_total += row_total
            self.oep_tree.insert("", "end", values=row_values)

        # Totals row
        totals_row = ["Total", ""] + [f"{t:,}" for t in col_totals] + [f"{grand_total:,}"]
        self.oep_tree.insert("", "end", values=totals_row, tags=("total",))
        self.oep_tree.tag_configure("total", font=("Segoe UI", 9, "bold"))

        self.oep_status_label.configure(
            text=(
                f"{len(pairs)} (country, division) pairs × {len(months)} months  ·  "
                f"{grand_total:,} workers"
            )
        )

    def _oep_render_full_report(self, result: dict) -> None:
        """Show a summary of every section the export will contain.

        The on-screen view is intentionally a compact index — the real
        deliverable is the multi-sheet Excel produced by Export.
        """
        cols = [("Section", 280), ("Rows", 80), ("Total", 140)]
        self._oep_set_columns(cols)

        country_summary = result["country_summary"]
        category_summary = result["category_summary"]
        division_summary = result["division_summary"]
        gender_summary = result["gender_summary"]
        months = result["months"]
        series = result["series"]
        divisions = result["divisions"]
        pivot_countries = result["pivot_countries"]
        table = result["table"]

        grand_total = sum(c.total_employee for c in country_summary)

        cdt_months = result.get("cdt_months", [])
        cdt_pairs = result.get("cdt_pairs", [])
        cdt_table = result.get("cdt_table", {})
        sections = [
            (
                "Cover (date range, headline totals)",
                "—",
                f"{grand_total:,} workers",
            ),
            (
                "By Country (every destination)",
                f"{len(country_summary)}",
                f"{grand_total:,}",
            ),
            (
                "By Division (Bangladesh source)",
                f"{len(division_summary)}",
                f"{sum(d.total_employee for d in division_summary):,}",
            ),
            (
                "By Category (every job)",
                f"{len(category_summary)}",
                f"{sum(c.total_employee for c in category_summary):,}",
            ),
            (
                "By Gender (per destination)",
                f"{len(gender_summary)}",
                f"{sum(g.total for g in gender_summary):,}",
            ),
            (
                "Monthly Time Series (selected countries)",
                f"{len(series)} × {len(months)} mo",
                f"{sum(sum(v) for v in series.values()):,}",
            ),
            (
                "Country × Division Pivot (selected countries)",
                f"{len(divisions)} × {len(pivot_countries)}",
                f"{sum(table.values()):,}",
            ),
            (
                "Country × Division × Month (flat sheet)",
                f"{len(cdt_pairs)} pairs × {len(cdt_months)} mo",
                f"{sum(cdt_table.values()):,}",
            ),
        ]
        for name, rows_count, total in sections:
            self.oep_tree.insert("", "end", values=(name, rows_count, total))

        self.oep_status_label.configure(
            text=(
                f"Full report ready — {grand_total:,} workers across "
                f"{len(country_summary)} destinations. Click Export to save the workbook."
            )
        )

    def _oep_render_pivot(self, result: dict) -> None:
        divisions: list[str] = result["divisions"]
        countries: list[str] = result["countries"]
        table: dict[tuple[str, str], int] = result["table"]

        # Trim long country labels for column headers so the grid fits.
        def _short(name: str, n: int = 14) -> str:
            return name if len(name) <= n else name[: n - 1] + "…"

        cols = [("Division", 140)] + [(_short(c), 110) for c in countries] + [("Total", 110)]
        self._oep_set_columns(cols)

        grand_total = 0
        for div in divisions:
            row_values = [div]
            row_total = 0
            for country in countries:
                v = table.get((div, country), 0)
                row_total += v
                row_values.append(f"{v:,}" if v else "—")
            row_values.append(f"{row_total:,}")
            grand_total += row_total
            self.oep_tree.insert("", "end", values=row_values)

        # Totals row
        totals = ["Total"]
        for country in countries:
            t = sum(table.get((d, country), 0) for d in divisions)
            totals.append(f"{t:,}")
        totals.append(f"{grand_total:,}")
        self.oep_tree.insert("", "end", values=totals, tags=("total",))
        self.oep_tree.tag_configure("total", font=("Segoe UI", 9, "bold"))

        self.oep_status_label.configure(
            text=(
                f"{len(divisions)} divisions × {len(countries)} destinations  ·  "
                f"{grand_total:,} total workers"
            )
        )

    def _oep_export(self) -> None:
        if not self._oep_last_result:
            return
        result = self._oep_last_result
        mode = result["mode"]
        suggested_dir = Path(self.oep_output_dir.get()) if self.oep_output_dir.get() else Path.home()
        target = filedialog.asksaveasfilename(
            title="Save OEP report",
            defaultextension=".xlsx",
            initialdir=str(suggested_dir),
            initialfile=excel_io.build_oep_output_path(suggested_dir, mode).name,
            filetypes=[("Excel files", "*.xlsx"), ("All files", "*.*")],
        )
        if not target:
            return
        path = Path(target)
        try:
            if mode == "country":
                excel_io.write_oep_country_report(
                    path,
                    date_from=result["date_from"], date_to=result["date_to"],
                    summary=result["summary"], raw_rows=result["raw"],
                )
            elif mode == "division":
                excel_io.write_oep_division_report(
                    path,
                    date_from=result["date_from"], date_to=result["date_to"],
                    summary=result["summary"], raw_rows=result["raw"],
                )
            elif mode == "category":
                excel_io.write_oep_category_report(
                    path,
                    date_from=result["date_from"], date_to=result["date_to"],
                    summary=result["summary"], raw_rows=result["raw"],
                )
            elif mode == "gender":
                excel_io.write_oep_gender_report(
                    path,
                    date_from=result["date_from"], date_to=result["date_to"],
                    summary=result["summary"],
                )
            elif mode == "timeseries":
                excel_io.write_oep_timeseries_report(
                    path,
                    date_from=result["date_from"], date_to=result["date_to"],
                    months=result["months"], series=result["series"],
                )
            elif mode == "pivot":
                excel_io.write_oep_pivot_report(
                    path,
                    date_from=result["date_from"], date_to=result["date_to"],
                    divisions=result["divisions"], countries=result["countries"],
                    table=result["table"],
                )
            elif mode == "cdt":
                excel_io.write_oep_country_division_timeseries(
                    path,
                    date_from=result["date_from"], date_to=result["date_to"],
                    months=result["months"], pairs=result["pairs"],
                    table=result["table"],
                )
            elif mode == "cdtd":
                excel_io.write_oep_country_district_timeseries(
                    path,
                    date_from=result["date_from"], date_to=result["date_to"],
                    months=result["months"], triples=result["triples"],
                    table=result["table"],
                )
            elif mode == "full":
                excel_io.write_oep_full_report(
                    path,
                    date_from=result["date_from"],
                    date_to=result["date_to"],
                    gender_id=result.get("gender_id", ""),
                    country_summary=result["country_summary"],
                    category_summary=result["category_summary"],
                    division_summary=result["division_summary"],
                    gender_summary=result["gender_summary"],
                    raw_country=result["raw_country"],
                    raw_division=result["raw_division"],
                    months=result["months"],
                    series=result["series"],
                    divisions=result["divisions"],
                    pivot_countries=result["pivot_countries"],
                    table=result["table"],
                    country_labels=result["country_labels"],
                    cdt_months=result.get("cdt_months"),
                    cdt_pairs=result.get("cdt_pairs"),
                    cdt_table=result.get("cdt_table"),
                )
        except Exception as exc:  # noqa: BLE001 — surface to user
            messagebox.showerror("OEP export", f"Could not save:\n{exc}")
            return
        # Remember the folder for next time
        self.oep_output_dir.set(str(path.parent))
        messagebox.showinfo("OEP export", f"Saved:\n{path}")

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
        # ------ OEP messages ------
        elif kind == MSG_OEP_BUSY:
            self.oep_status_label.configure(text=str(payload))
        elif kind == MSG_OEP_LOG:
            self.oep_status_label.configure(text=str(payload))
        elif kind == MSG_OEP_DONE:
            self._oep_render_results(payload)  # type: ignore[arg-type]
        elif kind == MSG_OEP_ERROR:
            self.oep_tree.delete(*self.oep_tree.get_children())
            self._oep_set_columns([("#", 40), ("Info", 600)])
            self.oep_tree.insert("", "end", values=("✕", str(payload)))
            self.oep_status_label.configure(text="Failed.")
            self.btn_oep_run.configure(state="normal")
            self.btn_oep_export.configure(state="disabled")
            messagebox.showerror("OEP", str(payload))
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
        # ------ Zenith tab messages ------
        elif kind == MSG_ZENITH_LOGGED_IN:
            sess = payload  # type: ignore[assignment]
            self._zenith_session = sess
            sv = sess.state_values
            self.zenith_login_status.configure(
                text=(
                    f"Signed in · user={sv.get('ID_ADMIN')} · "
                    f"company={sv.get('ID_SOCIETE')} · app={sv.get('ID_APPLICATION')}"
                ),
            )
            self.btn_zenith_login.configure(state="normal", text="Re-sign in")
            # Don't keep the password in the widget after success.
            self.zenith_pwd_entry.delete(0, "end")
            self._zenith_log("Signed in to Zenith.")
        elif kind == MSG_ZENITH_LOGIN_FAILED:
            self.btn_zenith_login.configure(state="normal", text="Sign in to Zenith")
            self.zenith_login_status.configure(text="Not signed in")
            messagebox.showerror("Zenith login failed", str(payload))
        elif kind == MSG_ZENITH_PROGRESS:
            completed, total_n, ok, nf, err = payload  # type: ignore[misc]
            self.zenith_progress_bar.configure(maximum=total_n, value=completed)
            pct = 100 * completed / total_n if total_n else 0
            self.zenith_progress_label.configure(
                text=(
                    f"{completed:,} / {total_n:,} ({pct:.1f}%)  ·  "
                    f"{ok:,} OK · {nf:,} not found · {err:,} errors"
                ),
            )
        elif kind == MSG_ZENITH_RESULT:
            r = payload  # type: ignore[assignment]
            if r.status == zenith_client.STATUS_OK and r.record is not None:
                self._zenith_log(
                    f"{r.checked_at}  {r.customer_id}  OK   "
                    f"{r.record.first_name} {r.record.last_name}  ·  {r.record.email}",
                )
            elif r.status == zenith_client.STATUS_NOT_FOUND:
                self._zenith_log(
                    f"{r.checked_at}  {r.customer_id}  NOT_FOUND",
                )
            else:
                self._zenith_log(
                    f"{r.checked_at}  {r.customer_id}  ERROR  {r.error}",
                )
        elif kind == MSG_ZENITH_DONE:
            path = str(payload)
            self._zenith_log(f"Done. Wrote {path}")
            self._refresh_zenith_cache_label()
            self._zenith_reset_buttons()
            messagebox.showinfo(
                "Zenith — Run Done", f"Finished.\n\nOutput:\n{path}",
            )
        elif kind == MSG_ZENITH_ERROR:
            self._zenith_log(f"ERROR: {payload}")
            self._zenith_reset_buttons()
            messagebox.showerror("Zenith — Run Error", str(payload))
        # ------ Zenith Flight Loads messages ------
        elif kind == MSG_ZENITH_FL_PROGRESS:
            chunk_label, done, total, rows = payload  # type: ignore[misc]
            self.zenith_fl_progress_bar.configure(maximum=total, value=done)
            self.zenith_fl_progress_label.configure(
                text=f"Chunk {done}/{total}  ·  {chunk_label}  ·  {rows:,} rows so far",
            )
            self._zenith_fl_log(
                f"Chunk {done}/{total} done: {chunk_label}  ({rows:,} rows total)",
            )
        elif kind == MSG_ZENITH_FL_DONE:
            path = str(payload)
            self._zenith_fl_log(f"Done. Wrote {path}")
            self._zenith_fl_reset_buttons()
            messagebox.showinfo(
                "Zenith Flight Loads — Done",
                f"Finished.\n\nOutput:\n{path}",
            )
        elif kind == MSG_ZENITH_FL_ERROR:
            self._zenith_fl_log(f"ERROR: {payload}")
            self._zenith_fl_reset_buttons()
            messagebox.showerror("Zenith Flight Loads — Error", str(payload))

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
            self.btn_sign_in.pack_forget()
            self.btn_sign_out.pack(
                side="right", padx=4, pady=2, before=self.btn_check_updates,
            )
        else:
            self.btn_sign_out.pack_forget()
            if auth.GOOGLE_CLIENT_ID:
                # CI build with the client ID baked in — offer sign in.
                self.status_user_label.configure(text="Not signed in")
                self.btn_sign_in.pack(
                    side="right", padx=4, pady=2, before=self.btn_check_updates,
                )
            else:
                # Dev build / fork with no client ID — auth is unavailable.
                # Don't offer a button that can't possibly succeed.
                self.status_user_label.configure(text="Unauthenticated (dev build)")
                self.btn_sign_in.pack_forget()

    def _on_sign_in(self) -> None:
        """Status-bar entry point for signing in after launch."""
        if self._signed_in_user:
            return
        if self._show_login_dialog():
            # _show_login_dialog already updates the label on success.
            pass

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


    # ==================================================================
    # Zenith Customer Lookup tab — UI
    # ==================================================================

    def _build_zenith_tab(self, parent: ttk.Frame) -> None:
        parent = self._make_scrollable(parent)

        intro = self._section(
            parent, "Zenith Customer Lookup  ·  asia.ttinteractive.com",
        )
        ttk.Label(
            intro, style="Hint.TLabel",
            text=(
                "Bulk-extract customer details (name, email, phone, address) "
                "for a list of Customer IDs. Read-only — never modifies "
                "Zenith data. Resume-safe: a SQLite cache lets you stop and "
                "restart without re-fetching."
            ),
            wraplength=900, justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 4))

        # ----- Login -----
        login_body = self._section(parent, "Sign in to Zenith")
        login_row = ttk.Frame(login_body)
        login_row.pack(fill="x", padx=2)
        self.zenith_user_entry = ttk.Entry(
            login_row, textvariable=self.zenith_username, width=24,
        )
        self.zenith_pwd_entry = ttk.Entry(
            login_row, show="•", width=24,
        )
        self.zenith_company_entry = ttk.Entry(
            login_row, textvariable=self.zenith_company, width=10,
        )
        ttk.Label(login_row, text="Username:").grid(
            row=0, column=0, padx=(0, 4), pady=4, sticky="w",
        )
        self.zenith_user_entry.grid(row=0, column=1, padx=(0, 12), pady=4)
        ttk.Label(login_row, text="Password:").grid(
            row=0, column=2, padx=(0, 4), pady=4, sticky="w",
        )
        self.zenith_pwd_entry.grid(row=0, column=3, padx=(0, 12), pady=4)
        ttk.Label(login_row, text="Company:").grid(
            row=0, column=4, padx=(0, 4), pady=4, sticky="w",
        )
        self.zenith_company_entry.grid(row=0, column=5, padx=(0, 12), pady=4)
        self.btn_zenith_login = ttk.Button(
            login_row, text="Sign in to Zenith",
            command=self._zenith_login, width=18,
        )
        self.btn_zenith_login.grid(row=0, column=6, padx=(0, 4), pady=4)
        self.zenith_login_status = ttk.Label(
            login_body, text="Not signed in", style="Hint.TLabel",
        )
        self.zenith_login_status.pack(anchor="w", padx=2, pady=(0, 4))

        # ----- Inner notebook so Customer Lookup and Flight Loads each
        # get their own canvas without polluting the other. Login above
        # remains shared.
        inner_nb = ttk.Notebook(parent)
        inner_nb.pack(fill="both", expand=True, padx=4, pady=(8, 0))
        customer_inner = ttk.Frame(inner_nb)
        flight_inner = ttk.Frame(inner_nb)
        inner_nb.add(customer_inner, text="Customer Lookup")
        inner_nb.add(flight_inner, text="Flight Loads")

        # From here, the existing Customer Lookup form goes into
        # `customer_inner` instead of the outer scroll frame.
        parent = customer_inner

        # ----- Input picker -----
        body = self._section(parent, "Input Excel")
        self.zenith_input_entry = ttk.Entry(
            body, textvariable=self.zenith_input_path,
        )
        ttk.Label(body, text="File:", width=14, anchor="w").grid(
            row=0, column=0, sticky="w", padx=(2, 4), pady=4,
        )
        self.zenith_input_entry.grid(row=0, column=1, sticky="ew", padx=(0, 4), pady=4)
        ttk.Button(body, text="Browse…", command=self._zenith_pick_input).grid(
            row=0, column=2, padx=(4, 2), pady=4,
        )
        self.zenith_sheet_combo = ttk.Combobox(
            body, textvariable=self.zenith_sheet_name, state="readonly",
            postcommand=self._zenith_reload_columns,
        )
        ttk.Label(body, text="Sheet:", width=14, anchor="w").grid(
            row=1, column=0, sticky="w", padx=(2, 4), pady=4,
        )
        self.zenith_sheet_combo.grid(row=1, column=1, sticky="ew", padx=(0, 4), pady=4)
        self.zenith_col_combo = ttk.Combobox(
            body, textvariable=self.zenith_column_name, state="readonly",
        )
        ttk.Label(body, text="ID column:", width=14, anchor="w").grid(
            row=2, column=0, sticky="w", padx=(2, 4), pady=4,
        )
        self.zenith_col_combo.grid(row=2, column=1, sticky="ew", padx=(0, 4), pady=4)
        body.columnconfigure(1, weight=1)

        # ----- Throughput controls -----
        speed_body = self._section(parent, "Throughput")
        ttk.Label(
            speed_body, style="Hint.TLabel",
            text=(
                "Concurrency = parallel HTTP connections (1 = polite, 10 = aggressive). "
                "Delay = pause between calls per worker. Start at 3 / 0.8 s; "
                "watch for any errors before increasing."
            ),
            wraplength=900, justify="left",
        ).pack(anchor="w", padx=2, pady=(0, 6))
        knobs = ttk.Frame(speed_body)
        knobs.pack(fill="x", padx=2)
        ttk.Label(knobs, text="Concurrency:").grid(
            row=0, column=0, padx=(0, 4), pady=4, sticky="w",
        )
        self.zenith_conc_scale = ttk.Scale(
            knobs, from_=1, to=10, orient="horizontal",
            variable=self.zenith_concurrency, length=200,
            command=lambda _v: self.zenith_concurrency.set(
                int(self.zenith_concurrency.get())
            ),
        )
        self.zenith_conc_scale.grid(row=0, column=1, padx=(0, 8), pady=4)
        self.zenith_conc_label = ttk.Label(knobs, text="3 workers")
        self.zenith_conc_label.grid(row=0, column=2, padx=(0, 24), pady=4)
        self.zenith_concurrency.trace_add(
            "write",
            lambda *_a: self.zenith_conc_label.configure(
                text=f"{int(self.zenith_concurrency.get())} workers",
            ),
        )

        ttk.Label(knobs, text="Delay (sec):").grid(
            row=0, column=3, padx=(0, 4), pady=4, sticky="w",
        )
        self.zenith_delay_scale = ttk.Scale(
            knobs, from_=0.1, to=2.0, orient="horizontal",
            variable=self.zenith_delay_s, length=200,
        )
        self.zenith_delay_scale.grid(row=0, column=4, padx=(0, 8), pady=4)
        self.zenith_delay_label = ttk.Label(knobs, text="0.8 s")
        self.zenith_delay_label.grid(row=0, column=5, padx=(0, 4), pady=4)
        self.zenith_delay_s.trace_add(
            "write",
            lambda *_a: self.zenith_delay_label.configure(
                text=f"{float(self.zenith_delay_s.get()):.1f} s",
            ),
        )

        # Cache + safety options
        opts = ttk.Frame(speed_body)
        opts.pack(fill="x", padx=2, pady=(8, 0))
        ttk.Checkbutton(
            opts, text="Skip IDs already in local cache",
            variable=self.zenith_skip_cached,
        ).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(
            opts, text="Test mode — first 100 IDs only",
            variable=self.zenith_test_mode,
        ).pack(side="left", padx=(0, 16))
        ttk.Button(
            opts, text="Reset cache…", command=self._zenith_reset_cache,
        ).pack(side="right")
        ttk.Button(
            opts, text="Retry failures", command=self._zenith_clear_errors,
        ).pack(side="right", padx=(0, 4))

        # ----- Output -----
        out_body = self._section(parent, "Output")
        self.zenith_output_entry = ttk.Entry(
            out_body, textvariable=self.zenith_output_dir,
        )
        ttk.Label(out_body, text="Folder:", width=14, anchor="w").grid(
            row=0, column=0, sticky="w", padx=(2, 4), pady=4,
        )
        self.zenith_output_entry.grid(
            row=0, column=1, sticky="ew", padx=(0, 4), pady=4,
        )
        ttk.Button(out_body, text="Browse…", command=self._zenith_pick_output).grid(
            row=0, column=2, padx=(4, 2), pady=4,
        )
        out_body.columnconfigure(1, weight=1)

        # ----- Run controls -----
        ctl = ttk.Frame(parent)
        ctl.pack(fill="x", padx=4, pady=(8, 0))
        self.btn_zenith_run = ttk.Button(
            ctl, text="Run", style="Primary.TButton",
            command=self._zenith_run,
        )
        self.btn_zenith_run.pack(side="left")
        self.btn_zenith_pause = ttk.Button(
            ctl, text="Pause", command=self._zenith_pause, state="disabled",
        )
        self.btn_zenith_pause.pack(side="left", padx=(8, 0))
        self.btn_zenith_resume = ttk.Button(
            ctl, text="Resume", command=self._zenith_resume, state="disabled",
        )
        self.btn_zenith_resume.pack(side="left", padx=(4, 0))
        self.btn_zenith_stop = ttk.Button(
            ctl, text="Stop", command=self._zenith_stop, state="disabled",
        )
        self.btn_zenith_stop.pack(side="left", padx=(4, 0))
        self.btn_zenith_export = ttk.Button(
            ctl, text="Export cache to Excel", command=self._zenith_export_cache,
        )
        self.btn_zenith_export.pack(side="left", padx=(16, 0))

        # ----- Progress -----
        prog_body = self._section(parent, "Progress")
        self.zenith_progress_bar = ttk.Progressbar(
            prog_body, mode="determinate", length=200,
        )
        self.zenith_progress_bar.pack(fill="x", padx=2, pady=(0, 4))
        self.zenith_progress_label = ttk.Label(
            prog_body, text="Idle.", style="Hint.TLabel",
        )
        self.zenith_progress_label.pack(anchor="w", padx=2)

        # ----- Log -----
        log_body = self._section(parent, "Log")
        log_frame = ttk.Frame(log_body)
        log_frame.pack(fill="both", expand=True, padx=2)
        self.zenith_log_text = tk.Text(
            log_frame, height=10, wrap="none", state="disabled",
            font=("Consolas", 9),
        )
        self.zenith_log_text.pack(side="left", fill="both", expand=True)
        log_scroll = ttk.Scrollbar(log_frame, command=self.zenith_log_text.yview)
        log_scroll.pack(side="right", fill="y")
        self.zenith_log_text.configure(yscrollcommand=log_scroll.set)

        self._refresh_zenith_cache_label()

        # ==============================================================
        # Flight Loads inner tab
        # ==============================================================
        self._build_zenith_flight_subtab(flight_inner)

    def _build_zenith_flight_subtab(self, parent: ttk.Frame) -> None:
        """Inner tab — date range pull from the View PNLs report."""
        # ----- Description -----
        intro = self._section(
            parent, "Flight Loads  ·  View PNLs",
        )
        ttk.Label(
            intro, style="Hint.TLabel",
            text=(
                "Pull flight-load data (tickets, seats, load %, status) "
                "for a date range. The server caps each search at 10 "
                "pages; this tab auto-chunks longer ranges into smaller "
                "windows so you can ask for up to ~12 months."
            ),
            wraplength=900, justify="left",
        ).pack(anchor="w", padx=4, pady=(0, 4))

        # ----- Range + page-size form -----
        form = self._section(parent, "Range")
        # State vars
        from datetime import date, timedelta
        today = date.today()
        a_week_ago = today - timedelta(days=6)
        self.zenith_fl_date_from = tk.StringVar(value=a_week_ago.strftime("%d/%m/%Y"))
        self.zenith_fl_date_to = tk.StringVar(value=today.strftime("%d/%m/%Y"))
        self.zenith_fl_page_size = tk.StringVar(value="100")
        self.zenith_fl_chunk_days = tk.IntVar(value=10)
        self.zenith_fl_delay_s = tk.DoubleVar(value=1.0)

        row1 = ttk.Frame(form)
        row1.pack(fill="x", padx=2)
        ttk.Label(row1, text="From (DD/MM/YYYY):").grid(
            row=0, column=0, padx=(0, 4), pady=4, sticky="w",
        )
        ttk.Entry(row1, textvariable=self.zenith_fl_date_from, width=14).grid(
            row=0, column=1, padx=(0, 12), pady=4,
        )
        ttk.Label(row1, text="To:").grid(
            row=0, column=2, padx=(0, 4), pady=4, sticky="w",
        )
        ttk.Entry(row1, textvariable=self.zenith_fl_date_to, width=14).grid(
            row=0, column=3, padx=(0, 12), pady=4,
        )
        ttk.Label(row1, text="Page size:").grid(
            row=0, column=4, padx=(0, 4), pady=4, sticky="w",
        )
        ttk.Combobox(
            row1, textvariable=self.zenith_fl_page_size,
            values=("20", "50", "100"), width=6, state="readonly",
        ).grid(row=0, column=5, padx=(0, 12), pady=4)
        ttk.Label(row1, text="Chunk (days):").grid(
            row=0, column=6, padx=(0, 4), pady=4, sticky="w",
        )
        ttk.Spinbox(
            row1, from_=1, to=30, textvariable=self.zenith_fl_chunk_days,
            width=5,
        ).grid(row=0, column=7, padx=(0, 12), pady=4)

        # ----- Throughput -----
        speed = self._section(parent, "Throughput")
        ttk.Label(
            speed, style="Hint.TLabel",
            text=(
                "Polite delay between paginated calls. The View PNLs page "
                "is heavier than the Customer page (~360 KB per call); "
                "1.0 s is a safe default."
            ),
            wraplength=900, justify="left",
        ).pack(anchor="w", padx=2, pady=(0, 4))
        knobs = ttk.Frame(speed)
        knobs.pack(fill="x", padx=2)
        ttk.Label(knobs, text="Delay (sec):").grid(
            row=0, column=0, padx=(0, 4), pady=4, sticky="w",
        )
        self.zenith_fl_delay_scale = ttk.Scale(
            knobs, from_=0.3, to=3.0, orient="horizontal",
            variable=self.zenith_fl_delay_s, length=200,
        )
        self.zenith_fl_delay_scale.grid(row=0, column=1, padx=(0, 8), pady=4)
        self.zenith_fl_delay_label = ttk.Label(knobs, text="1.0 s")
        self.zenith_fl_delay_label.grid(row=0, column=2, padx=(0, 4), pady=4)
        self.zenith_fl_delay_s.trace_add(
            "write",
            lambda *_a: self.zenith_fl_delay_label.configure(
                text=f"{float(self.zenith_fl_delay_s.get()):.1f} s",
            ),
        )

        # ----- Output -----
        out_body = self._section(parent, "Output")
        self.zenith_fl_output_dir = tk.StringVar(
            value=str(Path.home() / "Documents"),
        )
        self.zenith_fl_output_entry = ttk.Entry(
            out_body, textvariable=self.zenith_fl_output_dir,
        )
        ttk.Label(out_body, text="Folder:", width=14, anchor="w").grid(
            row=0, column=0, sticky="w", padx=(2, 4), pady=4,
        )
        self.zenith_fl_output_entry.grid(
            row=0, column=1, sticky="ew", padx=(0, 4), pady=4,
        )
        ttk.Button(
            out_body, text="Browse…", command=self._zenith_fl_pick_output,
        ).grid(row=0, column=2, padx=(4, 2), pady=4)
        out_body.columnconfigure(1, weight=1)

        # ----- Run controls -----
        ctl = ttk.Frame(parent)
        ctl.pack(fill="x", padx=4, pady=(8, 0))
        self.btn_zenith_fl_run = ttk.Button(
            ctl, text="Run", style="Primary.TButton",
            command=self._zenith_fl_run,
        )
        self.btn_zenith_fl_run.pack(side="left")
        self.btn_zenith_fl_stop = ttk.Button(
            ctl, text="Stop", command=self._zenith_fl_stop, state="disabled",
        )
        self.btn_zenith_fl_stop.pack(side="left", padx=(8, 0))

        # ----- Progress + log -----
        prog_body = self._section(parent, "Progress")
        self.zenith_fl_progress_bar = ttk.Progressbar(
            prog_body, mode="determinate", length=200,
        )
        self.zenith_fl_progress_bar.pack(fill="x", padx=2, pady=(0, 4))
        self.zenith_fl_progress_label = ttk.Label(
            prog_body, text="Idle.", style="Hint.TLabel",
        )
        self.zenith_fl_progress_label.pack(anchor="w", padx=2)

        log_body = self._section(parent, "Log")
        log_frame = ttk.Frame(log_body)
        log_frame.pack(fill="both", expand=True, padx=2)
        self.zenith_fl_log_text = tk.Text(
            log_frame, height=10, wrap="none", state="disabled",
            font=("Consolas", 9),
        )
        self.zenith_fl_log_text.pack(side="left", fill="both", expand=True)
        fl_scroll = ttk.Scrollbar(
            log_frame, command=self.zenith_fl_log_text.yview,
        )
        fl_scroll.pack(side="right", fill="y")
        self.zenith_fl_log_text.configure(yscrollcommand=fl_scroll.set)

        # ----- Worker state -----
        self._zenith_fl_worker: threading.Thread | None = None
        self._zenith_fl_stop_flag = threading.Event()

    # ==================================================================
    # Zenith Customer Lookup tab — actions
    # ==================================================================

    def _refresh_zenith_cache_label(self) -> None:
        counts = self._zenith_cache.counts_by_status()
        ok = counts.get(zenith_client.STATUS_OK, 0)
        nf = counts.get(zenith_client.STATUS_NOT_FOUND, 0)
        err = counts.get(zenith_client.STATUS_ERROR, 0)
        total = ok + nf + err
        if total == 0:
            text = "Cache is empty."
        else:
            text = f"Cache: {ok:,} OK · {nf:,} not found · {err:,} errors  ({total:,} total)"
        self.zenith_progress_label.configure(text=text)

    def _zenith_pick_input(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Excel with Customer IDs",
            filetypes=[("Excel files", "*.xlsx *.xlsm"), ("All files", "*.*")],
        )
        if not path:
            return
        self.zenith_input_path.set(path)
        try:
            sheets = excel_io.list_sheet_names(Path(path))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Zenith", f"Could not read sheets: {exc}")
            return
        self.zenith_sheet_combo.configure(values=sheets)
        if sheets:
            self.zenith_sheet_name.set(sheets[0])
            self._zenith_reload_columns()

    def _zenith_pick_output(self) -> None:
        folder = filedialog.askdirectory(title="Choose output folder")
        if folder:
            self.zenith_output_dir.set(folder)

    def _zenith_reload_columns(self) -> None:
        path = self.zenith_input_path.get().strip()
        sheet = self.zenith_sheet_name.get().strip()
        if not path or not sheet:
            return
        try:
            cols = excel_io.list_columns(Path(path), sheet)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Zenith", f"Could not read columns: {exc}")
            return
        self._zenith_sheet_columns = cols
        self.zenith_col_combo.configure(values=cols)
        if cols and not self.zenith_column_name.get():
            # Common defaults the user might have
            for guess in ("Customer ID", "CustomerID", "ID", "id"):
                if guess in cols:
                    self.zenith_column_name.set(guess)
                    break
            else:
                self.zenith_column_name.set(cols[0])

    def _zenith_login(self) -> None:
        user = self.zenith_username.get().strip()
        pwd = self.zenith_pwd_entry.get()
        company = self.zenith_company.get().strip() or "usba"
        if not user or not pwd:
            messagebox.showerror(
                "Zenith", "Enter both username and password.",
            )
            return
        self.btn_zenith_login.configure(state="disabled", text="Signing in…")
        self.zenith_login_status.configure(text="Connecting…")

        def worker() -> None:
            try:
                sess = zenith_client.ZenithSession.from_credentials(
                    user, pwd, company_code=company,
                )
                self._post(MSG_ZENITH_LOGGED_IN, sess)
            except zenith_client.LoginError as exc:
                self._post(MSG_ZENITH_LOGIN_FAILED, str(exc))
            except Exception as exc:  # noqa: BLE001
                log.exception("Zenith login crashed")
                self._post(MSG_ZENITH_LOGIN_FAILED, f"{type(exc).__name__}: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def _zenith_run(self) -> None:
        if self._zenith_session is None:
            messagebox.showerror(
                "Zenith", "Sign in to Zenith first.",
            )
            return
        in_path = self.zenith_input_path.get().strip()
        sheet = self.zenith_sheet_name.get().strip()
        col = self.zenith_column_name.get().strip()
        if not in_path or not sheet or not col:
            messagebox.showerror(
                "Zenith", "Pick an input file, sheet, and ID column first.",
            )
            return
        out_dir = Path(self.zenith_output_dir.get().strip() or str(Path.home()))

        # Read IDs
        try:
            ids = excel_io.read_zenith_ids(Path(in_path), sheet, col)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Zenith", f"Could not read IDs: {exc}")
            return
        if not ids:
            messagebox.showerror("Zenith", "No IDs found in that column.")
            return
        if self.zenith_test_mode.get():
            ids = ids[:100]

        # Optionally skip cached
        if self.zenith_skip_cached.get():
            cached = self._zenith_cache.cached_ids(only_ok=True)
            skipped = len([i for i in ids if i in cached])
            ids = [i for i in ids if i not in cached]
            if skipped:
                self._zenith_log(f"Skipping {skipped:,} IDs already in cache (OK).")
            if not ids:
                messagebox.showinfo(
                    "Zenith",
                    "All requested IDs are already cached. Nothing to fetch.\n\n"
                    "Use 'Export cache to Excel' to write them out.",
                )
                return

        # Big-run confirmation
        if len(ids) > 5000 and not messagebox.askyesno(
            "Zenith — large run",
            f"About to fetch {len(ids):,} customer records.\n\n"
            f"At concurrency={int(self.zenith_concurrency.get())} and "
            f"delay={float(self.zenith_delay_s.get()):.1f}s, this could take "
            f"a few hours. Continue?",
        ):
            return

        # Worker setup
        self._zenith_stop_flag.clear()
        self._zenith_pause_flag.clear()
        out_path = excel_io.build_zenith_output_path(out_dir)
        cfg = {
            "ids": ids,
            "concurrency": int(self.zenith_concurrency.get()),
            "delay_s": float(self.zenith_delay_s.get()),
            "out_path": out_path,
        }
        self._zenith_worker = threading.Thread(
            target=self._zenith_worker_run, args=(cfg,), daemon=True,
        )
        self._zenith_log(
            f"Run starting — {len(ids):,} IDs · "
            f"concurrency={cfg['concurrency']} · delay={cfg['delay_s']:.1f}s",
        )
        self.zenith_progress_bar.configure(value=0, maximum=len(ids))
        self.btn_zenith_run.configure(state="disabled")
        self.btn_zenith_pause.configure(state="normal")
        self.btn_zenith_stop.configure(state="normal")
        self._zenith_worker.start()

    def _zenith_worker_run(self, cfg: dict) -> None:
        ids = cfg["ids"]
        total = len(ids)
        ok = nf = err = 0

        def progress_cb(result, completed: int, total_n: int) -> None:
            nonlocal ok, nf, err
            self._zenith_cache.save_result(result)
            if result.status == zenith_client.STATUS_OK:
                ok += 1
            elif result.status == zenith_client.STATUS_NOT_FOUND:
                nf += 1
            else:
                err += 1
            self._post(MSG_ZENITH_PROGRESS, (completed, total_n, ok, nf, err))
            self._post(MSG_ZENITH_RESULT, result)

        try:
            zenith_client.fetch_many(
                self._zenith_session, ids,
                concurrency=cfg["concurrency"],
                delay_s=cfg["delay_s"],
                progress_cb=progress_cb,
                stop_event=self._zenith_stop_flag,
                pause_event=self._zenith_pause_flag,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Zenith run crashed")
            self._post(MSG_ZENITH_ERROR, f"{type(exc).__name__}: {exc}")
            return

        # Write everything cached to Excel
        try:
            excel_io.write_zenith_results(
                cfg["out_path"], self._zenith_cache.iter_all(),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Zenith Excel write failed")
            self._post(MSG_ZENITH_ERROR, f"Excel write failed: {exc}")
            return
        self._post(MSG_ZENITH_DONE, str(cfg["out_path"]))

    def _zenith_pause(self) -> None:
        self._zenith_pause_flag.set()
        self.btn_zenith_pause.configure(state="disabled")
        self.btn_zenith_resume.configure(state="normal")
        self._zenith_log("Paused.")

    def _zenith_resume(self) -> None:
        self._zenith_pause_flag.clear()
        self.btn_zenith_resume.configure(state="disabled")
        self.btn_zenith_pause.configure(state="normal")
        self._zenith_log("Resumed.")

    def _zenith_stop(self) -> None:
        self._zenith_stop_flag.set()
        self._zenith_pause_flag.clear()
        self._zenith_log("Stopping after current batch finishes…")
        self.btn_zenith_stop.configure(state="disabled")

    def _zenith_reset_cache(self) -> None:
        counts = self._zenith_cache.counts_by_status()
        total = sum(counts.values())
        if not total:
            messagebox.showinfo("Zenith", "Cache already empty.")
            return
        if not messagebox.askyesno(
            "Reset cache?",
            f"Delete all {total:,} cached customer records?\n\n"
            "Useful for starting fresh. The Excel files you've already "
            "exported are NOT affected.",
        ):
            return
        self._zenith_cache.reset()
        self._refresh_zenith_cache_label()
        self._zenith_log("Cache reset.")

    def _zenith_clear_errors(self) -> None:
        dropped = self._zenith_cache.clear_errors()
        self._refresh_zenith_cache_label()
        self._zenith_log(
            f"Cleared {dropped:,} error rows — they'll be retried on the next run.",
        )

    def _zenith_export_cache(self) -> None:
        out_dir = Path(self.zenith_output_dir.get().strip() or str(Path.home()))
        path = excel_io.build_zenith_output_path(out_dir)
        try:
            excel_io.write_zenith_results(path, self._zenith_cache.iter_all())
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Zenith", f"Excel write failed: {exc}")
            return
        self._zenith_log(f"Exported cache to {path}")
        messagebox.showinfo("Zenith — Export Done", f"Wrote:\n\n{path}")

    def _zenith_log(self, text: str) -> None:
        self.zenith_log_text.configure(state="normal")
        self.zenith_log_text.insert("end", text + "\n")
        self.zenith_log_text.see("end")
        self.zenith_log_text.configure(state="disabled")

    def _zenith_reset_buttons(self) -> None:
        self.btn_zenith_run.configure(state="normal")
        self.btn_zenith_pause.configure(state="disabled")
        self.btn_zenith_resume.configure(state="disabled")
        self.btn_zenith_stop.configure(state="disabled")

    # ==================================================================
    # Zenith Flight Loads sub-tab — actions
    # ==================================================================

    def _zenith_fl_log(self, text: str) -> None:
        self.zenith_fl_log_text.configure(state="normal")
        self.zenith_fl_log_text.insert("end", text + "\n")
        self.zenith_fl_log_text.see("end")
        self.zenith_fl_log_text.configure(state="disabled")

    def _zenith_fl_pick_output(self) -> None:
        folder = filedialog.askdirectory(title="Choose flight-loads output folder")
        if folder:
            self.zenith_fl_output_dir.set(folder)

    def _zenith_fl_run(self) -> None:
        if self._zenith_session is None:
            messagebox.showerror(
                "Zenith", "Sign in to Zenith first (top of this tab).",
            )
            return
        date_from = self.zenith_fl_date_from.get().strip()
        date_to = self.zenith_fl_date_to.get().strip()
        if not date_from or not date_to:
            messagebox.showerror(
                "Zenith", "Enter both From and To dates (DD/MM/YYYY).",
            )
            return
        try:
            from datetime import datetime
            datetime.strptime(date_from, "%d/%m/%Y")
            datetime.strptime(date_to, "%d/%m/%Y")
        except ValueError:
            messagebox.showerror(
                "Zenith", "Dates must be in DD/MM/YYYY format.",
            )
            return

        try:
            page_size = int(self.zenith_fl_page_size.get())
        except ValueError:
            page_size = 100
        chunk_days = max(1, int(self.zenith_fl_chunk_days.get()))
        delay_s = float(self.zenith_fl_delay_s.get())
        out_dir = Path(
            self.zenith_fl_output_dir.get().strip() or str(Path.home()),
        )
        out_path = excel_io.build_zenith_flight_output_path(out_dir)

        self._zenith_fl_stop_flag.clear()
        self.btn_zenith_fl_run.configure(state="disabled")
        self.btn_zenith_fl_stop.configure(state="normal")
        self.zenith_fl_progress_bar.configure(value=0, maximum=1)
        self._zenith_fl_log(
            f"Flight loads run starting: {date_from} → {date_to} · "
            f"page_size={page_size} · chunk={chunk_days}d · delay={delay_s:.1f}s",
        )

        cfg = {
            "date_from": date_from, "date_to": date_to,
            "page_size": page_size, "chunk_days": chunk_days,
            "delay_s": delay_s, "out_path": out_path,
        }
        self._zenith_fl_worker = threading.Thread(
            target=self._zenith_fl_worker_run, args=(cfg,), daemon=True,
        )
        self._zenith_fl_worker.start()

    def _zenith_fl_worker_run(self, cfg: dict) -> None:
        def progress_cb(chunk_label, done, total, rows_so_far):
            self._post(
                MSG_ZENITH_FL_PROGRESS,
                (chunk_label, done, total, rows_so_far),
            )

        try:
            rows = zenith_client.fetch_flight_loads(
                self._zenith_session,
                cfg["date_from"], cfg["date_to"],
                page_size=cfg["page_size"],
                chunk_days=cfg["chunk_days"],
                inter_call_delay_s=cfg["delay_s"],
                progress_cb=progress_cb,
                stop_event=self._zenith_fl_stop_flag,
            )
        except zenith_client.SessionExpiredError as exc:
            self._post(MSG_ZENITH_FL_ERROR, f"Session expired — sign in again. ({exc})")
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("Zenith flight-loads run crashed")
            self._post(MSG_ZENITH_FL_ERROR, f"{type(exc).__name__}: {exc}")
            return

        if not rows:
            self._post(
                MSG_ZENITH_FL_ERROR,
                "No flights found for that date range (or the run was stopped).",
            )
            return

        try:
            excel_io.write_zenith_flight_loads(cfg["out_path"], rows)
        except Exception as exc:  # noqa: BLE001
            log.exception("Flight-loads Excel write failed")
            self._post(MSG_ZENITH_FL_ERROR, f"Excel write failed: {exc}")
            return
        self._post(MSG_ZENITH_FL_DONE, str(cfg["out_path"]))

    def _zenith_fl_stop(self) -> None:
        self._zenith_fl_stop_flag.set()
        self._zenith_fl_log("Stopping after current page finishes…")
        self.btn_zenith_fl_stop.configure(state="disabled")

    def _zenith_fl_reset_buttons(self) -> None:
        self.btn_zenith_fl_run.configure(state="normal")
        self.btn_zenith_fl_stop.configure(state="disabled")


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
