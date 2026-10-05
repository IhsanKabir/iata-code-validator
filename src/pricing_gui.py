"""The Zenith "Pricing" sub-tab: the read-only season check.

Compares the current season's fares, taxes and penalties with the next
season's (or every later one) through pricing_check, which can only READ
Zenith -- pricing_client refuses anything that could save. Password-gated
like FFP Customers, with the same password. Host must provide `self.root`,
`self._post`, `self._section`, `self._form_row`, `self._make_scrollable`,
the signed-in `self._zenith_session`, and route "pricing_" messages to
`self._pricing_handle_msg`.
"""
from __future__ import annotations

import logging
import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

log = logging.getLogger(__name__)

PRICING_MSG_LOG = "pricing_log"            # payload: str
PRICING_MSG_DONE = "pricing_done"          # payload: (path, CheckResult)
PRICING_MSG_INDEX = "pricing_index"        # payload: (fare bases, built)
PRICING_MSG_ERROR = "pricing_error"        # payload: str

_TITLE = "Pricing"


class PricingMixin:
    # ---- building ---------------------------------------------------------
    def _build_zenith_pricing_tab(self, parent: ttk.Frame) -> None:
        """A password screen first; the tab itself is built on unlock."""
        self._pricing_worker: threading.Thread | None = None
        self._pricing_stop = threading.Event()
        self._pricing_last_path = ""
        self._pricing_unlocked = False
        self._pricing_parent = parent
        gate = ttk.Frame(parent)
        gate.pack(anchor="nw", padx=16, pady=16)
        self._pricing_gate = gate
        ttk.Label(gate, text="Pricing is password-protected.",
                  font=("Segoe UI Semibold", 11)).pack(anchor="w")
        row = ttk.Frame(gate)
        row.pack(anchor="w", pady=(8, 0))
        ttk.Label(row, text="Password:").pack(side="left", padx=(0, 6))
        self.pricing_pwd = ttk.Entry(row, show="•", width=24)
        self.pricing_pwd.pack(side="left")
        self.pricing_pwd.bind("<Return>", lambda _e: self._pricing_unlock())
        ttk.Button(row, text="Unlock", style="Primary.TButton",
                   command=self._pricing_unlock).pack(side="left", padx=(8, 0))
        self.pricing_gate_msg = ttk.Label(gate, text="", foreground="#B00020")
        self.pricing_gate_msg.pack(anchor="w", pady=(6, 0))

    def _pricing_unlock(self) -> None:
        from . import tab_lock
        if self._pricing_unlocked:
            return
        entered = self.pricing_pwd.get()
        self.pricing_pwd.delete(0, "end")      # never left on screen
        if not tab_lock.check(entered):
            self.pricing_gate_msg.configure(text="Wrong password.")
            return
        self._pricing_unlocked = True
        self._pricing_gate.destroy()
        self._pricing_build_contents(self._pricing_parent)

    def _pricing_build_contents(self, parent: ttk.Frame) -> None:
        parent = self._make_scrollable(parent)
        self._section(
            parent, "Season check  ·  is the next season set up like this one?",
            help_text=(
                "Reads Zenith and compares the current season with the next "
                "one (or every later season):\n\n"
                "• Fares — every Fare Editor row of every enabled fare basis: "
                "currency, OW/RT amounts, GDS amounts, baggage and the rest.\n"
                "• Sales check — Quick Sale must sell each adult fare at the "
                "Fare Editor's amount, in its own currency.\n"
                "• Taxes — the tax screen's list per route (YQ/YR included), "
                "and Quick Sale's total tax per fare.\n"
                "• Penalties — change, refund and no-show fees per fare "
                "class, route, passenger type and time window.\n\n"
                "Every difference is listed. READ ONLY: the app cannot save "
                "anything in Zenith from here — any request that could is "
                "refused before it is sent.\n\n"
                "The route list (which fare bases have fares on which route) "
                "is built once by reading every fare basis — press 'Rebuild "
                "route list' the first time, and again after fare bases are "
                "added or routes change."))

        box = self._section(parent, "What to check")
        self.pricing_routes = tk.StringVar(value="")
        row = ttk.Frame(box)
        ttk.Entry(row, textvariable=self.pricing_routes, width=50
                  ).pack(side="left")
        ttk.Label(row, text="  e.g. DAC-CCU, DAC-DXB — leave empty for ALL "
                            "routes", style="Hint.TLabel").pack(side="left")
        self._form_row(box, 0, "Routes:", row, label_width=18)
        self.pricing_return = tk.BooleanVar(value=True)
        self._form_row(box, 1, "", ttk.Checkbutton(
            box, text="Include the return direction (DAC-CCU → also CCU-DAC)",
            variable=self.pricing_return), label_width=18)
        self.pricing_mode = tk.StringVar(value="next")
        row = ttk.Frame(box)
        ttk.Radiobutton(row, text="Current vs next season", value="next",
                        variable=self.pricing_mode).pack(side="left")
        ttk.Radiobutton(row, text="Current vs ALL later seasons", value="all",
                        variable=self.pricing_mode).pack(side="left",
                                                         padx=(12, 0))
        self._form_row(box, 2, "Seasons:", row, label_width=18)
        self.pricing_parts = {p: tk.BooleanVar(value=True)
                              for p in ("Fares", "Taxes", "Penalties")}
        row = ttk.Frame(box)
        for p, var in self.pricing_parts.items():
            ttk.Checkbutton(row, text=p, variable=var).pack(side="left",
                                                            padx=(0, 10))
        ttk.Label(row, text="Fares includes the Quick Sale sales check",
                  style="Hint.TLabel").pack(side="left")
        self._form_row(box, 3, "Parts:", row, label_width=18)
        self.pricing_output = tk.StringVar(value=str(Path.home() /
                                                     "Documents"))
        self._form_row(box, 4, "Output folder:",
                       ttk.Entry(box, textvariable=self.pricing_output),
                       label_width=18,
                       suffix=ttk.Button(box, text="Browse…",
                                         command=self._pricing_pick_output))

        ctl = ttk.Frame(parent)
        ctl.pack(fill="x", padx=4, pady=(8, 4))
        self.btn_pricing_run = ttk.Button(ctl, text="Run season check",
                                          style="Primary.TButton",
                                          command=self._pricing_run)
        self.btn_pricing_run.pack(side="left")
        self.btn_pricing_stop = ttk.Button(ctl, text="Stop", state="disabled",
                                           style="Danger.TButton",
                                           command=self._pricing_stop_clicked)
        self.btn_pricing_stop.pack(side="left", padx=(8, 0))
        self.btn_pricing_index = ttk.Button(ctl, text="Rebuild route list",
                                            command=self._pricing_rebuild)
        self.btn_pricing_index.pack(side="left", padx=(8, 0))
        self.btn_pricing_open = ttk.Button(ctl, text="Open workbook",
                                           state="disabled",
                                           command=self._pricing_open)
        self.btn_pricing_open.pack(side="left", padx=(8, 0))

        self.pricing_index_lbl = ttk.Label(parent, text="",
                                           style="Hint.TLabel")
        self.pricing_index_lbl.pack(anchor="w", padx=8)
        self.pricing_status = ttk.Label(parent, text="Sign in to Zenith "
                                        "above, then press Run.",
                                        style="Hint.TLabel")
        self.pricing_status.pack(anchor="w", padx=8)
        self.pricing_summary = ttk.Label(parent, text="", justify="left",
                                         wraplength=900)
        self.pricing_summary.pack(anchor="w", padx=8, pady=(4, 0))
        self.pricing_warning = ttk.Label(parent, text="", style="Hint.TLabel",
                                         wraplength=900, justify="left",
                                         foreground="#B00020")
        self.pricing_warning.pack(anchor="w", padx=8)
        self._pricing_show_index()

    # ---- the route list ---------------------------------------------------
    @staticmethod
    def _pricing_index():
        from . import config
        from .pricing_check import RouteIndex
        return RouteIndex(Path(config.APP_DIR) / "pricing_routes.json")

    def _pricing_show_index(self) -> None:
        idx = self._pricing_index()
        if idx.load():
            self.pricing_index_lbl.configure(
                text=f"Route list: {len(idx.legs):,} fare bases, built "
                     f"{idx.built}.")
        else:
            self.pricing_index_lbl.configure(
                text="Route list not built yet — press 'Rebuild route list' "
                     "once (it reads every fare basis; it can take a while).")

    # ---- actions ----------------------------------------------------------
    def _pricing_pick_output(self) -> None:
        got = filedialog.askdirectory(
            title="Where should the season check workbook go?",
            initialdir=self.pricing_output.get() or str(Path.home()))
        if got:
            self.pricing_output.set(got)

    def _pricing_busy(self) -> bool:
        return (self._pricing_worker is not None
                and self._pricing_worker.is_alive())

    def _pricing_ready(self) -> bool:
        if self._pricing_busy():
            return False
        if getattr(self, "_zenith_session", None) is None:
            messagebox.showerror(_TITLE, "Sign in to Zenith first (top of "
                                         "this tab).")
            return False
        return True

    def _pricing_start(self, target, args, msg: str) -> None:
        self._pricing_stop.clear()
        self.pricing_warning.configure(text="")
        for b in (self.btn_pricing_run, self.btn_pricing_index):
            b.configure(state="disabled")
        self.btn_pricing_stop.configure(state="normal")
        self._pricing_log(msg)
        self._pricing_worker = threading.Thread(target=target, args=args,
                                                daemon=True)
        self._pricing_worker.start()

    def _pricing_client(self):
        from . import zenith_client
        from .pricing_client import PricingClient
        return PricingClient(self._zenith_session,
                             base_url=zenith_client.BASE_URL)

    def _pricing_run(self) -> None:
        if not self._pricing_ready():
            return
        from .pricing_check import Settings
        parts = {p: v.get() for p, v in self.pricing_parts.items()}
        if not any(parts.values()):
            messagebox.showerror(_TITLE, "Tick at least one part.")
            return
        if not self._pricing_index().load():
            messagebox.showerror(_TITLE, "Build the route list first — "
                                         "press 'Rebuild route list'.")
            return
        settings = Settings(routes_text=self.pricing_routes.get(),
                            include_return=bool(self.pricing_return.get()),
                            mode=self.pricing_mode.get(),
                            fares=parts["Fares"], taxes=parts["Taxes"],
                            penalties=parts["Penalties"])
        out_dir = Path(self.pricing_output.get().strip() or str(Path.home()))
        self.pricing_summary.configure(text="")
        self._pricing_start(self._pricing_worker_run, (settings, out_dir),
                            "Reading Zenith…")

    def _pricing_worker_run(self, settings, out_dir: Path) -> None:
        from . import pricing_check as pc
        from . import pricing_report as pr
        from .route_optimisation_report import writable_path
        try:
            idx = self._pricing_index()
            idx.load()
            res = pc.run_check(self._pricing_client(), idx, settings,
                               stop=self._pricing_stop,
                               progress=lambda m: self._post(PRICING_MSG_LOG,
                                                             m))
            out_dir.mkdir(parents=True, exist_ok=True)
            path = writable_path(out_dir / pr.default_filename())
            pr.build_workbook(res, path)
            self._post(PRICING_MSG_DONE, (str(path), res))
        except InterruptedError:
            self._post(PRICING_MSG_ERROR, "Stopped — no workbook written.")
        except Exception as exc:             # noqa: BLE001
            log.exception("Season check failed")
            self._post(PRICING_MSG_ERROR, _explain(exc))

    def _pricing_rebuild(self) -> None:
        if not self._pricing_ready():
            return
        if not messagebox.askyesno(
                _TITLE, "Rebuild the route list? It reads every enabled fare "
                        "basis in every season still to come — thousands of "
                        "reads, which can take an hour or more."):
            return
        self._pricing_start(self._pricing_rebuild_run, (),
                            "Building the route list…")

    def _pricing_rebuild_run(self) -> None:
        from . import pricing_check as pc
        try:
            idx = self._pricing_index()
            n = pc.rebuild_index(self._pricing_client(), idx,
                                 stop=self._pricing_stop,
                                 progress=lambda m: self._post(
                                     PRICING_MSG_LOG, m))
            self._post(PRICING_MSG_INDEX, (n, idx.built))
        except InterruptedError:
            self._post(PRICING_MSG_ERROR, "Stopped — the previous route list "
                                          "is kept.")
        except Exception as exc:             # noqa: BLE001
            log.exception("Route list rebuild failed")
            self._post(PRICING_MSG_ERROR, _explain(exc))

    def _pricing_stop_clicked(self) -> None:
        self._pricing_stop.set()
        self.btn_pricing_stop.configure(state="disabled")
        self._pricing_log("Stopping after the current read…")

    def _pricing_open(self) -> None:
        if self._pricing_last_path:
            try:
                os.startfile(self._pricing_last_path)  # noqa: S606 — user asked
            except Exception as exc:                   # noqa: BLE001
                messagebox.showerror(_TITLE, str(exc))

    # ---- messages ---------------------------------------------------------
    def _pricing_log(self, msg: str) -> None:
        try:
            self.pricing_status.configure(text=msg)
        except Exception:                    # noqa: BLE001 - UI only
            pass

    def _pricing_finish(self) -> None:
        for b in (self.btn_pricing_run, self.btn_pricing_index):
            b.configure(state="normal")
        self.btn_pricing_stop.configure(state="disabled")

    def _pricing_handle_msg(self, kind: str, payload) -> bool:
        if not kind.startswith("pricing_"):
            return False
        if not getattr(self, "_pricing_unlocked", False):
            return True                # nothing to show on a locked tab
        if kind == PRICING_MSG_LOG:
            self._pricing_log(str(payload))
        elif kind == PRICING_MSG_DONE:
            path, res = payload
            self._pricing_finish()
            self._pricing_last_path = path
            self.btn_pricing_open.configure(state="normal")
            self._pricing_log(f"Done — saved {Path(path).name}.")
            self.pricing_summary.configure(text=summary_text(res))
            if res.problems:
                self.pricing_warning.configure(
                    text=f"{len(res.problems):,} item(s) could not be "
                         f"checked — see the 'Not checked' sheet.")
        elif kind == PRICING_MSG_INDEX:
            n, built = payload
            self._pricing_finish()
            self._pricing_show_index()
            self._pricing_log(f"Route list built: {n:,} fare bases.")
        elif kind == PRICING_MSG_ERROR:
            self._pricing_finish()
            self._pricing_log("Stopped — the reason is shown below.")
            self.pricing_warning.configure(text=str(payload))
        else:
            return False
        return True


def summary_text(res) -> str:
    from .pricing_report import PARTS
    lines = [f"{res.base}  vs  {', '.join(res.others)}  ·  "
             f"{len(res.routes):,} route(s)"]
    for part in PARTS:
        compared = res.compared.get(part, 0)
        if not compared:
            continue
        found = sum(res.count(part).values())
        lines.append(f"{part}: {compared:,} compared — "
                     + ("all match" if not found else
                        f"{found:,} to look at"))
    return "\n".join(lines)


def _explain(exc: Exception) -> str:
    from . import pricing_client as cl
    from . import pricing_parse as pp
    if isinstance(exc, cl.SessionLost):
        return (f"{exc} The route list is saved — no need to rebuild it; "
                f"just sign in and press Run again.")
    if isinstance(exc, cl.ReadOnlyViolation):
        return (f"Refused before sending: {exc} Nothing was changed in "
                f"Zenith — please report this.")
    if isinstance(exc, pp.LayoutChanged):
        return (f"A Zenith screen looks different from when this check was "
                f"built ({exc}). Stopped rather than misread it.")
    return f"{type(exc).__name__}: {exc}"
