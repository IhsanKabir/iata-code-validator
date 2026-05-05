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

from . import config, excel_io
from .cache import Cache
from .parser import LookupResult, now_iso
from .validator import (
    CaptchaChallenge,
    IATAValidator,
    ValidatorStopped,
    make_validator,
)

log = logging.getLogger(__name__)


# Worker → GUI message types
MSG_LOG = "log"
MSG_PROGRESS = "progress"
MSG_RESULT = "result"
MSG_CAPTCHA = "captcha"
MSG_DONE = "done"
MSG_ERROR = "error"


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("IATA CheckACode — Bulk Validator")
        self.root.geometry("900x680")
        self.root.minsize(820, 600)

        # State
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
        self._pause_event.set()  # set = running, clear = paused
        self._stop_flag = threading.Event()
        self._captcha_clear_flag = threading.Event()
        self._msg_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()

        self._build_ui()
        self.root.after(100, self._poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        # Top frame: file pickers
        frm = ttk.LabelFrame(self.root, text="Input")
        frm.pack(fill="x", padx=10, pady=(10, 4))

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
        out = ttk.LabelFrame(self.root, text="Output")
        out.pack(fill="x", padx=10, pady=4)
        ttk.Label(out, text="Folder:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(out, textvariable=self.output_dir).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(out, text="Browse...", command=self._pick_output).grid(row=0, column=2, **pad)
        out.columnconfigure(1, weight=1)

        # Controls
        ctrl = ttk.Frame(self.root)
        ctrl.pack(fill="x", padx=10, pady=4)
        self.btn_start = ttk.Button(ctrl, text="Start", command=self._start)
        self.btn_start.pack(side="left", padx=4)
        self.btn_pause = ttk.Button(ctrl, text="Pause", command=self._pause, state="disabled")
        self.btn_pause.pack(side="left", padx=4)
        self.btn_resume = ttk.Button(ctrl, text="Resume", command=self._resume, state="disabled")
        self.btn_resume.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(ctrl, text="Stop", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=4)

        # Progress
        progress_frm = ttk.LabelFrame(self.root, text="Progress")
        progress_frm.pack(fill="x", padx=10, pady=4)
        self.progress_bar = ttk.Progressbar(progress_frm, mode="determinate")
        self.progress_bar.pack(fill="x", padx=8, pady=6)
        self.progress_label = ttk.Label(progress_frm, text="Idle.")
        self.progress_label.pack(anchor="w", padx=8, pady=(0, 6))

        # Log
        log_frm = ttk.LabelFrame(self.root, text="Log")
        log_frm.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.log_text = tk.Text(log_frm, height=18, wrap="none", font=("Consolas", 9))
        self.log_text.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        scroll = ttk.Scrollbar(log_frm, command=self.log_text.yview)
        scroll.pack(side="right", fill="y", pady=8, padx=(0, 8))
        self.log_text.configure(yscrollcommand=scroll.set, state="disabled")

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
        if self._worker is not None and self._worker.is_alive():
            if not messagebox.askyesno("Quit?", "A run is in progress. Quit anyway?"):
                return
            self._stop_flag.set()
            self._pause_event.set()
            self._captcha_clear_flag.set()
            if self._validator is not None:
                self._validator.stop()
            # Give the worker a moment to clean up.
            self._worker.join(timeout=5)
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
    App(root)
    root.mainloop()
