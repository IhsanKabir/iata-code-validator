"""Shared Health / diagnostics panel, mixed into both apps.

Builds a "Run checks" button + a live status grid (one row per feature, colored
OK/WARN/FAIL with a fix hint) + an overall banner. Runs the checks on a worker
thread and streams results back through the host's message queue, so a slow
network probe never freezes the window. Host must provide: `self.root`,
`self._post(kind, payload)`, `self._COLOR_*`, and route "health_" messages to
`self._health_handle_msg`.
"""

from __future__ import annotations

import logging
import threading
import tkinter as tk
from tkinter import ttk

from . import health_checks as hc

log = logging.getLogger(__name__)

HEALTH_MSG_RESULT = "health_result"    # payload: HealthResult
HEALTH_MSG_DONE = "health_done"        # payload: summary dict


class HealthMixin:
    def _build_health_panel(self, parent: tk.Widget, app_name: str) -> None:
        self._health_app = app_name
        self._health_worker: threading.Thread | None = None
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True, padx=6, pady=6)

        ttk.Label(
            wrap, style="Hint.TLabel", justify="left", wraplength=900,
            text=("Checks whether each feature can actually work right now — the "
                  "local engine (browser, caches, report stack) and whether each "
                  "external site it depends on is reachable. Green = good, amber = "
                  "works but watch it, red = broken (with a fix hint)."),
        ).pack(anchor="w", pady=(0, 6))

        ctl = ttk.Frame(wrap)
        ctl.pack(fill="x")
        self.btn_health_run = ttk.Button(
            ctl, text="Run all checks", style="Primary.TButton",
            command=self._health_run)
        self.btn_health_run.pack(side="left")
        self.health_overall = ttk.Label(ctl, text="Not run yet.", style="Hint.TLabel")
        self.health_overall.pack(side="left", padx=(12, 0))

        cols = ("feature", "category", "status", "detail", "fix")
        self.health_tree = ttk.Treeview(wrap, columns=cols, show="headings", height=12)
        for cid, txt, w in (("feature", "Feature", 230), ("category", "Type", 100),
                            ("status", "Status", 70), ("detail", "Detail", 260),
                            ("fix", "Fix hint", 300)):
            self.health_tree.heading(cid, text=txt)
            self.health_tree.column(cid, width=w, anchor="w")
        self.health_tree.tag_configure("ok", background="#DFF6DD")
        self.health_tree.tag_configure("warn", background="#FFF4CE")
        self.health_tree.tag_configure("fail", background="#FDE7E9")
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.health_tree.xview)
        hsb.pack(side="bottom", fill="x")
        vsb = ttk.Scrollbar(wrap, command=self.health_tree.yview)
        vsb.pack(side="right", fill="y")
        self.health_tree.pack(fill="both", expand=True, pady=(6, 0))
        self.health_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        if hasattr(self, "_register_result_tree"):
            self._register_result_tree(self.health_tree)
        self._health_iids: dict = {}

    def _open_health_dialog(self, app_name: str) -> None:
        """Standalone entry point: a diagnostics window (no dedicated tab)."""
        if getattr(self, "_health_win", None) is not None:
            try:
                self._health_win.deiconify()
                self._health_win.lift()
                return
            except tk.TclError:
                self._health_win = None
        win = tk.Toplevel(self.root)
        win.title("Health / Diagnostics")
        win.geometry("980x560")
        self._health_win = win

        def _closed() -> None:
            self._health_win = None
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", _closed)
        self._build_health_panel(win, app_name)

    # -- run ----------------------------------------------------------------

    def _health_run(self) -> None:
        if self._health_worker and self._health_worker.is_alive():
            return
        for c in self.health_tree.get_children():
            self.health_tree.delete(c)
        self._health_iids = {}
        self.btn_health_run.configure(state="disabled")
        self.health_overall.configure(text="Running checks…")
        app = self._health_app

        def worker() -> None:
            try:
                results = hc.run_health_checks(
                    app, timeout=10.0, verify_browser=True,
                    on_result=lambda r: self._post(HEALTH_MSG_RESULT, r))
                self._post(HEALTH_MSG_DONE, hc.summarize(results))
            except Exception as exc:  # noqa: BLE001
                log.exception("health run failed")
                self._post(HEALTH_MSG_DONE, {"error": f"{type(exc).__name__}: {exc}"})

        self._health_worker = threading.Thread(target=worker, daemon=True)
        self._health_worker.start()

    # -- dispatch -----------------------------------------------------------

    def _health_handle_msg(self, kind: str, payload) -> bool:
        # The standalone Health dialog can be closed mid-run; if its widgets are
        # gone, consume the message (return True) but touch nothing — otherwise a
        # TclError escapes the queue pump and freezes ALL background messaging.
        tree = getattr(self, "health_tree", None)
        if tree is None or not tree.winfo_exists():
            return kind.startswith("health_")
        if kind == HEALTH_MSG_RESULT:
            r = payload
            tag = {hc.OK: "ok", hc.WARN: "warn", hc.FAIL: "fail"}.get(r.status, "")
            glyph = {hc.OK: "✓ OK", hc.WARN: "▲ WARN", hc.FAIL: "✕ FAIL"}.get(r.status, r.status)
            vals = (r.feature, r.category, glyph, r.detail, r.remedy)
            iid = self._health_iids.get(r.key)
            if iid and self.health_tree.exists(iid):
                self.health_tree.item(iid, values=vals, tags=(tag,))
            else:
                self._health_iids[r.key] = self.health_tree.insert(
                    "", "end", values=vals, tags=(tag,))
            return True
        if kind == HEALTH_MSG_DONE:
            self.btn_health_run.configure(state="normal")
            if isinstance(payload, dict) and payload.get("error"):
                self.health_overall.configure(text=f"Error: {payload['error']}")
                return True
            s = payload
            fail, warn = s.get("fail", 0), s.get("warn", 0)
            if not fail and not warn:
                txt, color = f"All {s.get('total', 0)} features healthy ✓", self._COLOR_SUCCESS
            elif fail:
                txt = f"{fail} broken, {warn} warning(s) — see red rows"
                color = self._COLOR_DANGER
            else:
                txt, color = f"{warn} warning(s) — all reachable", self._COLOR_WARNING
            self.health_overall.configure(text=txt, foreground=color)
            return True
        return False
