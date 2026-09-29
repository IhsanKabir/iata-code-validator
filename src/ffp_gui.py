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
FFP_MSG_LIMIT = "ffp_limit"          # payload: dict found, or None

_TITLE = "FFP Customers"


class FFPMixin:
    # ---- building ---------------------------------------------------------
    def _build_zenith_ffp_tab(self, parent: ttk.Frame) -> None:
        """A password screen first; the tab itself is built on unlock."""
        self._ffp_worker: threading.Thread | None = None
        self._ffp_stop = threading.Event()
        self._ffp_store_obj = None
        self._ffp_last_path = ""
        self._ffp_unlocked = False
        self._ffp_parent = parent
        gate = ttk.Frame(parent)
        gate.pack(anchor="nw", padx=16, pady=16)
        self._ffp_gate = gate
        ttk.Label(gate, text="FFP Customers is password-protected.",
                  font=("Segoe UI Semibold", 11)).pack(anchor="w")
        ttk.Label(gate, text="The member list holds customers' personal "
                             "details.", style="Hint.TLabel"
                  ).pack(anchor="w", pady=(0, 8))
        row = ttk.Frame(gate)
        row.pack(anchor="w")
        ttk.Label(row, text="Password:").pack(side="left", padx=(0, 6))
        self.ffp_pwd = ttk.Entry(row, show="•", width=24)
        self.ffp_pwd.pack(side="left")
        self.ffp_pwd.bind("<Return>", lambda _e: self._ffp_unlock())
        ttk.Button(row, text="Unlock", style="Primary.TButton",
                   command=self._ffp_unlock).pack(side="left", padx=(8, 0))
        self.ffp_gate_msg = ttk.Label(gate, text="", foreground="#B00020")
        self.ffp_gate_msg.pack(anchor="w", pady=(6, 0))

    def _ffp_unlock(self) -> None:
        from . import tab_lock
        if self._ffp_unlocked:
            return
        entered = self.ffp_pwd.get()
        self.ffp_pwd.delete(0, "end")          # never left on screen
        if not tab_lock.check(entered):
            self.ffp_gate_msg.configure(text="Wrong password.")
            return
        self._ffp_unlocked = True
        self._ffp_gate.destroy()
        self._ffp_build_contents(self._ffp_parent)

    def _ffp_build_contents(self, parent: ttk.Frame) -> None:
        parent = self._make_scrollable(parent)
        self._section(
            parent, "FFP Customers  ·  every frequent-flyer member",
            help_text=(
                "Collects FFP members -- number, name, date of birth, "
                "email, phone, ID number, level and miles -- from Zenith's "
                "FFP account search.\n\n"
                "That search lists at most 50 members at a time but always "
                "says how many matched. So the app narrows it -- by the "
                "start of the last name within each level, since Zenith "
                "matches FFP numbers only whole -- until every search lists "
                "all it found.\n\n"
                "Levels go in order Gold, Platinum, Titanium, then Silver. "
                "Silver has about 120,000 members and needs tens of "
                "thousands of searches -- days of running -- so it is off "
                "until you tick it.\n\n"
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
        self.ffp_delay = tk.DoubleVar(value=0.5)
        row = ttk.Frame(io)
        ttk.Spinbox(row, from_=0, to=10, increment=0.5, width=5,
                    textvariable=self.ffp_delay).pack(side="left")
        ttk.Label(row, text="  seconds between searches — raise it if "
                            "searches start timing out",
                  style="Hint.TLabel").pack(side="left")
        self._form_row(io, 1, "Pause:", row, label_width=18)
        self.ffp_workers = tk.IntVar(value=3)
        row = ttk.Frame(io)
        ttk.Spinbox(row, from_=1, to=4, increment=1, width=5,
                    textvariable=self.ffp_workers).pack(side="left")
        ttk.Label(row, text="  searches at once — overlaps the wait for "
                            "Zenith; drop to 1 if it starts refusing",
                  style="Hint.TLabel").pack(side="left")
        self._form_row(io, 2, "At once:", row, label_width=18)

        from .ffp_collect import LEVEL_ORDER
        self.ffp_levels = {lv: tk.BooleanVar(value=lv != "Silver")
                           for lv in LEVEL_ORDER}
        row = ttk.Frame(io)
        for lv in LEVEL_ORDER:
            ttk.Checkbutton(row, text=lv, variable=self.ffp_levels[lv]
                            ).pack(side="left", padx=(0, 10))
        ttk.Label(row, text="Silver: ~120,000 members — days of searching",
                  style="Hint.TLabel").pack(side="left")
        self._form_row(io, 3, "Levels:", row, label_width=18)

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
        self.btn_ffp_limit = ttk.Button(ctl, text="Test the 50 limit",
                                        command=self._ffp_test_limit)
        self.btn_ffp_limit.pack(side="left", padx=(8, 0))

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
                self._ffp_render(fc.progress_of(
                    store, levels=self._ffp_ticked()))
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

    def _ffp_ticked(self) -> list:
        return [lv for lv, var in getattr(self, "ffp_levels", {}).items()
                if var.get()]

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
            delay = max(0.0, float(self.ffp_delay.get()))
            workers = min(4, max(1, int(self.ffp_workers.get())))
        except (tk.TclError, ValueError):
            messagebox.showerror(_TITLE, "'Pause' and 'At once' must be "
                                         "numbers.")
            return
        levels = [lv for lv, var in self.ffp_levels.items() if var.get()]
        if not levels:
            messagebox.showerror(_TITLE, "Tick at least one level.")
            return
        self._ffp_stop.clear()
        self.ffp_warning.configure(text="")
        self.btn_ffp_run.configure(state="disabled")
        self.btn_ffp_limit.configure(state="disabled")
        self.btn_ffp_stop.configure(state="normal")
        self._ffp_log("Opening Zenith's FFP search…")
        self._ffp_worker = threading.Thread(
            target=self._ffp_worker_run, args=(delay, workers, levels),
            daemon=True)
        self._ffp_worker.start()

    def _ffp_worker_run(self, delay: float, workers: int = 1,
                        levels=None) -> None:
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
                delay_s=delay, workers=workers, levels=levels)
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

    def _ffp_test_limit(self) -> None:
        """Does Zenith list more than 50 rows if a search asks for 5000?"""
        if self._ffp_busy():
            messagebox.showinfo(_TITLE, "Stop the collection first.")
            return
        if getattr(self, "_zenith_session", None) is None:
            messagebox.showerror(_TITLE, "Sign in to Zenith first (top of "
                                         "this tab).")
            return
        self._ffp_stop.clear()
        self.ffp_warning.configure(text="")
        for b in (self.btn_ffp_run, self.btn_ffp_limit):
            b.configure(state="disabled")
        self.btn_ffp_stop.configure(state="normal")
        self._ffp_log("Testing whether Zenith lists more than 50 rows — "
                      "about 40 searches…")

        def work() -> None:
            from . import ffp_collect as fc
            from . import zenith_client
            from . import zenith_ffp as zf
            searcher = zf.FFPSearcher(self._zenith_session,
                                      base_url=zenith_client.BASE_URL)
            try:
                got = fc.probe_page_limit(
                    searcher, self._ffp_store(), stop=self._ffp_stop,
                    log=lambda m: self._post(FFP_MSG_LOG, m))
                self._post(FFP_MSG_LIMIT, got)
            except Exception as exc:         # noqa: BLE001
                log.exception("FFP limit test failed")
                self._post(FFP_MSG_ERROR, f"The test stopped: {exc}")

        self._ffp_worker = threading.Thread(target=work, daemon=True)
        self._ffp_worker.start()

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
        self.btn_ffp_limit.configure(state="normal")
        self.btn_ffp_stop.configure(state="disabled")
        self.btn_ffp_export.configure(state="normal")

    def _ffp_handle_msg(self, kind: str, payload) -> bool:
        if not kind.startswith("ffp_"):
            return False
        if not getattr(self, "_ffp_unlocked", False):
            return True                # nothing to show on a locked tab
        if kind == FFP_MSG_LOG:
            self._ffp_log(str(payload))
        elif kind == FFP_MSG_PROGRESS:
            self._ffp_render(payload)
            level = payload.last.split("|", 1)[0] if "|" in payload.last                 else ""
            if level:
                self._ffp_log(f"Collecting {level}…")
        elif kind == FFP_MSG_DONE:
            self._ffp_render(payload)
            self._ffp_finish()
            from . import ffp_collect as fc
            store = self._ffp_store()
            left = fc.pending_levels(store)
            chosen = [lv for lv, var in self.ffp_levels.items() if var.get()]
            if not left:
                self._ffp_log("Collection complete — press Export to Excel.")
            elif not set(left) & set(chosen):
                self._ffp_log(f"{', '.join(chosen)} complete — press Export "
                              f"to Excel. Not collected yet: "
                              f"{', '.join(left)}.")
            else:
                self._ffp_log("Stopped. Press Collect to carry on from "
                              "here.")
        elif kind == FFP_MSG_ERROR:
            self._ffp_finish()
            self._ffp_log("Stopped — the reason is shown below.")
            self.ffp_warning.configure(text=str(payload))
            # what the run did learn -- the level counts at least -- stays
            # on screen; the message refers to it
            if self._ffp_store_path().is_file():
                from . import ffp_collect as fc
                self._ffp_render(fc.progress_of(self._ffp_store(),
                                                levels=self._ffp_ticked()))
        elif kind == FFP_MSG_LIMIT:
            self._ffp_finish()
            from . import ffp_collect as fc
            self._ffp_render(fc.progress_of(self._ffp_store(),
                                                levels=self._ffp_ticked()))
            if payload:
                self._ffp_log(
                    f"Zenith lists up to {payload['listed']:,} rows when "
                    f"asked ({payload['name']}). Press Collect — whole "
                    f"levels now come in one search.")
            else:
                self._ffp_log("Zenith lists 50 rows whatever is asked — "
                              "the limit cannot be lifted from here.")
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
