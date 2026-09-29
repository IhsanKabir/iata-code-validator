"""The Zenith "FFP Customers" sub-tab: every frequent-flyer member.

Collects through ffp_collect -- Zenith's FFP search narrowed until it lists
everything, since it shows at most 50 rows a search -- in the background,
keeping progress as it goes so Stop or a lost session costs nothing. Host
must provide `self.root`, `self._post`, `self._section`, `self._form_row`,
`self._make_scrollable`, `self._register_result_tree`, the signed-in
`self._zenith_session`, and route "ffp_" messages to `self._ffp_handle_msg`.
"""
from __future__ import annotations

import logging
import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

log = logging.getLogger(__name__)

FFP_MSG_LOG = "ffp_log"              # payload: str
FFP_MSG_PROGRESS = "ffp_progress"    # payload: ffp_collect.Progress
FFP_MSG_DONE = "ffp_done"            # payload: ffp_collect.Progress
FFP_MSG_ERROR = "ffp_error"          # payload: str
FFP_MSG_EXPORTED = "ffp_exported"    # payload: (path, rows)

_TITLE = "FFP Customers"


class FFPMixin:
    # ---- building ---------------------------------------------------------
    def _build_zenith_ffp_tab(self, parent: ttk.Frame) -> None:
        self._ffp_worker: threading.Thread | None = None
        self._ffp_stop = threading.Event()
        self._ffp_store_obj = None
        self._ffp_last_path = ""
        parent = self._make_scrollable(parent)
        self._section(
            parent, "FFP Customers  ·  every frequent-flyer member",
            help_text=(
                "Collects every FFP member -- number, name, date of birth, "
                "email, phone, ID number, level and miles -- from Zenith's "
                "FFP account search.\n\n"
                "That search lists at most 50 members at a time but always "
                "says how many matched. So the app searches FFP numbers "
                "beginning 0-9, splits any beginning that matches more than "
                "50 into ten longer ones, and repeats until every search "
                "lists all it found.\n\n"
                "It first counts each level in Zenith, so the finished list "
                "is checked against Zenith's own numbers.\n\n"
                "Progress is saved as it goes: Stop, a crash or a signed-out "
                "session loses nothing -- press Collect again to carry on. "
                "The list holds customers' personal details and stays on "
                "this machine until you export it."))

        io = self._section(parent, "Output")
        self.ffp_output = tk.StringVar(value=str(Path.home() / "Documents"))
        self._form_row(io, 0, "Output folder:",
                       ttk.Entry(io, textvariable=self.ffp_output),
                       label_width=18,
                       suffix=ttk.Button(io, text="Browse…",
                                         command=self._ffp_pick_output))
        self.ffp_delay = tk.DoubleVar(value=1.0)
        row = ttk.Frame(io)
        ttk.Spinbox(row, from_=0.5, to=10, increment=0.5, width=5,
                    textvariable=self.ffp_delay).pack(side="left")
        ttk.Label(row, text="  seconds between searches — gentler on "
                            "Zenith; raise it if searches start timing out",
                  style="Hint.TLabel").pack(side="left")
        self._form_row(io, 1, "Pause:", row, label_width=18)

        ctl = ttk.Frame(parent)
        ctl.pack(fill="x", padx=4, pady=(8, 4))
        self.btn_ffp_run = ttk.Button(ctl, text="Collect FFP members",
                                      style="Primary.TButton",
                                      command=self._ffp_run)
        self.btn_ffp_run.pack(side="left")
        self.btn_ffp_stop = ttk.Button(ctl, text="Stop", state="disabled",
                                       style="Danger.TButton",
                                       command=self._ffp_stop_clicked)
        self.btn_ffp_stop.pack(side="left", padx=(8, 0))
        self.btn_ffp_export = ttk.Button(ctl, text="Export to Excel",
                                         command=self._ffp_export)
        self.btn_ffp_export.pack(side="left", padx=(8, 0))
        self.btn_ffp_open = ttk.Button(ctl, text="Open workbook",
                                       state="disabled", command=self._ffp_open)
        self.btn_ffp_open.pack(side="left", padx=(8, 0))
        ttk.Button(ctl, text="Start over", command=self._ffp_reset
                   ).pack(side="left", padx=(8, 0))

        self.ffp_status = ttk.Label(parent, text="Sign in to Zenith above, "
                                    "then press Collect.", style="Hint.TLabel")
        self.ffp_status.pack(anchor="w", padx=8)
        self.ffp_progress_lbl = ttk.Label(parent, text="", style="Hint.TLabel")
        self.ffp_progress_lbl.pack(anchor="w", padx=8)
        self.ffp_warning = ttk.Label(parent, text="", style="Hint.TLabel",
                                     wraplength=900, justify="left",
                                     foreground="#B00020")
        self.ffp_warning.pack(anchor="w", padx=8)

        body = self._section(parent, "By level")
        cols = (("level", "Level", 140), ("zenith", "In Zenith", 120),
                ("found", "Collected", 120), ("left", "Still to find", 120))
        self.ffp_tree = ttk.Treeview(body, columns=[c[0] for c in cols],
                                     show="headings", height=6)
        for cid, label, width in cols:
            self.ffp_tree.heading(cid, text=label)
            self.ffp_tree.column(cid, width=width,
                                 anchor="w" if cid == "level" else "e")
        self.ffp_tree.pack(fill="x", padx=2, pady=4)
        self._register_result_tree(self.ffp_tree)
        self._ffp_show_saved()

    # ---- the store --------------------------------------------------------
    @staticmethod
    def _ffp_store_path() -> Path:
        from . import config
        return Path(config.APP_DIR) / "ffp_members.sqlite"

    def _ffp_store(self):
        if self._ffp_store_obj is None:
            from . import ffp_collect as fc
            self._ffp_store_obj = fc.FFPStore(self._ffp_store_path())
        return self._ffp_store_obj

    def _ffp_show_saved(self) -> None:
        """What an earlier run left, so the tab opens where it stopped.
        Opening the tab creates nothing: only a run makes the file."""
        if not self._ffp_store_path().is_file():
            return
        try:
            from . import ffp_collect as fc
            store = self._ffp_store()
            if store.member_count() or store.frontier_counts():
                self._ffp_render(fc.progress_of(store))
                done = store.get("finished") == "1"
                self._ffp_log("Collection complete — export any time."
                              if done else "An earlier collection stopped "
                              "part-way — press Collect to carry on.")
        except Exception:                    # noqa: BLE001 - UI only
            log.exception("Could not read saved FFP progress")

    # ---- actions ----------------------------------------------------------
    def _ffp_pick_output(self) -> None:
        got = filedialog.askdirectory(
            title="Where should the FFP workbook go?",
            initialdir=self.ffp_output.get() or str(Path.home()))
        if got:
            self.ffp_output.set(got)

    def _ffp_busy(self) -> bool:
        return self._ffp_worker is not None and self._ffp_worker.is_alive()

    def _ffp_run(self) -> None:
        if self._ffp_busy():
            return
        if getattr(self, "_zenith_session", None) is None:
            messagebox.showerror(_TITLE, "Sign in to Zenith first (top of "
                                         "this tab).")
            return
        try:
            delay = max(0.5, float(self.ffp_delay.get()))
        except (tk.TclError, ValueError):
            messagebox.showerror(_TITLE, "'Pause' must be a number of "
                                         "seconds.")
            return
        self._ffp_stop.clear()
        self.ffp_warning.configure(text="")
        self.btn_ffp_run.configure(state="disabled")
        self.btn_ffp_stop.configure(state="normal")
        self._ffp_log("Opening Zenith's FFP search…")
        self._ffp_worker = threading.Thread(
            target=self._ffp_worker_run, args=(delay,), daemon=True)
        self._ffp_worker.start()

    def _ffp_worker_run(self, delay: float) -> None:
        from . import ffp_collect as fc
        from . import zenith_client
        from . import zenith_ffp as zf

        searcher = zf.FFPSearcher(self._zenith_session,
                                  base_url=zenith_client.BASE_URL)
        try:
            got = fc.collect(
                searcher, self._ffp_store(), stop=self._ffp_stop,
                log=lambda m: self._post(FFP_MSG_LOG, m),
                on_progress=lambda p: self._post(FFP_MSG_PROGRESS, p),
                delay_s=delay)
            self._post(FFP_MSG_DONE, got)
        except fc.PartialMatchUnsupported as exc:
            self._post(FFP_MSG_ERROR, str(exc))
        except zf.FFPSearchError as exc:
            log.warning("FFP collection stopped: %s", exc)
            self._post(FFP_MSG_ERROR, f"{exc}  Progress so far is kept.")
        except Exception as exc:             # noqa: BLE001
            log.exception("FFP collection failed")
            self._post(FFP_MSG_ERROR, f"{type(exc).__name__}: {exc}  "
                                      "Progress so far is kept.")

    def _ffp_stop_clicked(self) -> None:
        self._ffp_stop.set()
        self.btn_ffp_stop.configure(state="disabled")
        self._ffp_log("Stopping after the current search…")

    def _ffp_export(self) -> None:
        if self._ffp_busy():
            messagebox.showinfo(_TITLE, "Stop the collection first, or wait "
                                        "for it to finish.")
            return
        store = self._ffp_store()
        if not store.member_count():
            messagebox.showinfo(_TITLE, "Nothing collected yet.")
            return
        out_dir = Path(self.ffp_output.get().strip() or str(Path.home()))
        self.btn_ffp_export.configure(state="disabled")
        self._ffp_log(f"Writing {store.member_count():,} members to Excel…")

        def work() -> None:
            from . import ffp_collect as fc
            from .route_optimisation_report import writable_path
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                path = writable_path(out_dir / fc.default_filename())
                rows = fc.export_workbook(store, path)
                self._post(FFP_MSG_EXPORTED, (str(path), rows))
            except Exception as exc:         # noqa: BLE001
                log.exception("FFP export failed")
                self._post(FFP_MSG_ERROR, f"Export failed: {exc}")

        threading.Thread(target=work, daemon=True).start()

    def _ffp_open(self) -> None:
        if self._ffp_last_path:
            try:
                os.startfile(self._ffp_last_path)  # noqa: S606 — user asked
            except Exception as exc:               # noqa: BLE001
                messagebox.showerror(_TITLE, str(exc))

    def _ffp_reset(self) -> None:
        if self._ffp_busy():
            messagebox.showinfo(_TITLE, "Stop the collection first.")
            return
        if not messagebox.askyesno(
                _TITLE, "Forget everything collected so far and start from "
                        "the beginning next time?"):
            return
        self._ffp_store().reset()
        self.ffp_tree.delete(*self.ffp_tree.get_children())
        self.ffp_progress_lbl.configure(text="")
        self._ffp_log("Cleared. Press Collect to start again.")

    # ---- messages ---------------------------------------------------------
    def _ffp_log(self, msg: str) -> None:
        try:
            self.ffp_status.configure(text=msg)
        except Exception:                    # noqa: BLE001 - UI only
            pass

    def _ffp_render(self, p) -> None:
        from . import zenith_ffp as zf
        expected = f" of {p.expected:,} in Zenith" if p.expected else ""
        self.ffp_progress_lbl.configure(
            text=f"Members collected: {p.members:,}{expected}   ·   "
                 f"searches: {p.searches:,}   ·   still to search: "
                 f"{p.pending:,}" + (f"   ·   last: {p.last}" if p.last
                                     else ""))
        t = self.ffp_tree
        t.delete(*t.get_children())
        for i, level in enumerate(zf.LEVELS):
            z = p.level_totals.get(level, -1)
            f = p.level_found.get(level, 0)
            t.insert("", "end", tags=("stripe",) if i % 2 else (), values=(
                level, "" if z < 0 else f"{z:,}", f"{f:,}",
                "" if z < 0 else f"{max(0, z - f):,}"))

    def _ffp_finish(self) -> None:
        self.btn_ffp_run.configure(state="normal")
        self.btn_ffp_stop.configure(state="disabled")
        self.btn_ffp_export.configure(state="normal")

    def _ffp_handle_msg(self, kind: str, payload) -> bool:
        if kind == FFP_MSG_LOG:
            self._ffp_log(str(payload))
        elif kind == FFP_MSG_PROGRESS:
            self._ffp_render(payload)
        elif kind == FFP_MSG_DONE:
            self._ffp_render(payload)
            self._ffp_finish()
            finished = self._ffp_store().get("finished") == "1"
            self._ffp_log(
                "Collection complete — press Export to Excel." if finished
                else "Stopped. Press Collect to carry on from here.")
        elif kind == FFP_MSG_ERROR:
            self._ffp_finish()
            self._ffp_log("Stopped — the reason is shown below.")
            self.ffp_warning.configure(text=str(payload))
            # what the run did learn -- the level counts at least -- stays
            # on screen; the message refers to it
            if self._ffp_store_path().is_file():
                from . import ffp_collect as fc
                self._ffp_render(fc.progress_of(self._ffp_store()))
        elif kind == FFP_MSG_EXPORTED:
            path, rows = payload
            self._ffp_last_path = path
            self.btn_ffp_export.configure(state="normal")
            self.btn_ffp_open.configure(state="normal")
            self._ffp_log(f"Saved {rows:,} members to {Path(path).name} — "
                          "confidential: holds personal details.")
        else:
            return False
        return True
