"""The "Tax setup check" section of the Pricing tab: is a tax set up on every
route of a kind (INT-INT, INT-DOM, DOM-INT, DOM-DOM) or on routes you name,
and set up the same way? READ ONLY.

Mixed into PricingMixin, which provides the fold-up section, the worker
plumbing (`_pricing_ready`, `_pricing_start`, `_pricing_client`,
`_pricing_stop`) and the shared status lines.
"""
from __future__ import annotations

import logging
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import messagebox, ttk

log = logging.getLogger(__name__)

PRICING_MSG_TAX_SETUP_DONE = "pricing_tax_setup_done"  # (path, result)

_TITLE = "Tax setup check"

TAX_SETUP_HELP = (
    "Checks the tax screen route by route, on one travel date:\n\n"
    "• Which taxes each route carries — pick route types (INT-INT, INT-DOM, "
    "DOM-INT, DOM-DOM; domestic = a Bangladesh airport) and/or name routes: "
    "AUH-DAC, AUH-* (every route from AUH), *-CGP (every route to CGP).\n"
    "• Tax codes (AE, ZR, YQ…) narrow it to those taxes and report every "
    "route that lacks them; leave empty for every tax.\n\n"
    "Inconsistencies found: a tax missing where most routes from the same "
    "airport have it, the same tax at different amounts, a tax set up twice "
    "on one route, entries Zenith marks not applicable, International / "
    "Domestic ticks that do not fit the route, zero amounts, and routes with "
    "no tax at all.\n\n"
    "Each pair of airports is one search (both directions): all route types "
    "together is about 700 searches, 5–10 minutes. READ ONLY — nothing can "
    "be changed in Zenith from here.")


class TaxSetupMixin:
    def _tax_setup_build(self, parent: ttk.Frame) -> None:
        from .tax_setup import KINDS
        box = self._section(parent, "What to check")
        self.tax_setup_codes = tk.StringVar(value="")
        row = ttk.Frame(box)
        ttk.Entry(row, textvariable=self.tax_setup_codes, width=30
                  ).pack(side="left")
        ttk.Label(row, text="  e.g. AE, ZR, E5 — leave empty for every tax",
                  style="Hint.TLabel").pack(side="left")
        self._form_row(box, 0, "Tax codes:", row, label_width=18)
        self.tax_setup_kinds = {k: tk.BooleanVar(value=True) for k in KINDS}
        row = ttk.Frame(box)
        for k, var in self.tax_setup_kinds.items():
            ttk.Checkbutton(row, text=k, variable=var).pack(side="left",
                                                            padx=(0, 10))
        ttk.Label(row, text="DOM = a Bangladesh airport",
                  style="Hint.TLabel").pack(side="left")
        self._form_row(box, 1, "Route types:", row, label_width=18)
        self.tax_setup_routes = tk.StringVar(value="")
        row = ttk.Frame(box)
        ttk.Entry(row, textvariable=self.tax_setup_routes, width=50
                  ).pack(side="left")
        ttk.Label(row, text="  e.g. AUH-DAC, AUH-*, *-CGP — empty = every "
                            "airport pair", style="Hint.TLabel"
                  ).pack(side="left")
        self._form_row(box, 2, "Routes:", row, label_width=18)
        self.tax_setup_return = tk.BooleanVar(value=False)
        self._form_row(box, 3, "", ttk.Checkbutton(
            box, text="Include the return direction of the routes named",
            variable=self.tax_setup_return), label_width=18)
        self.tax_setup_date = tk.StringVar(value=date.today().strftime(
            "%d/%m/%Y"))
        row = ttk.Frame(box)
        ttk.Entry(row, textvariable=self.tax_setup_date, width=12
                  ).pack(side="left")
        ttk.Label(row, text="  dd/mm/yyyy — the taxes in force for travel "
                            "on this day", style="Hint.TLabel"
                  ).pack(side="left")
        self._form_row(box, 4, "Travel date:", row, label_width=18)

        ctl = ttk.Frame(parent)
        ctl.pack(fill="x", padx=4, pady=(8, 4))
        self.btn_tax_setup_run = ttk.Button(ctl, text="Run tax check",
                                            style="Primary.TButton",
                                            command=self._tax_setup_run)
        self.btn_tax_setup_run.pack(side="left")
        self.btn_tax_setup_stop = ttk.Button(
            ctl, text="Stop", state="disabled", style="Danger.TButton",
            command=self._pricing_stop_clicked)
        self.btn_tax_setup_stop.pack(side="left", padx=(8, 0))
        self.btn_tax_setup_open = ttk.Button(ctl, text="Open workbook",
                                             state="disabled",
                                             command=self._pricing_open)
        self.btn_tax_setup_open.pack(side="left", padx=(8, 0))

    def _tax_setup_settings(self):
        """The form -> TaxSetupSettings; ValueError says what is wrong."""
        from datetime import datetime

        from .tax_setup import TaxSetupSettings, parse_codes
        kinds = tuple(k for k, v in self.tax_setup_kinds.items() if v.get())
        if not kinds:
            raise ValueError("Tick at least one route type.")
        parse_codes(self.tax_setup_codes.get())
        day = self.tax_setup_date.get().strip()
        try:
            datetime.strptime(day, "%d/%m/%Y")
        except ValueError:
            raise ValueError("Travel date must be dd/mm/yyyy, e.g. "
                             "03/11/2026.") from None
        return TaxSetupSettings(codes_text=self.tax_setup_codes.get(),
                                routes_text=self.tax_setup_routes.get(),
                                include_return=bool(
                                    self.tax_setup_return.get()),
                                kinds=kinds, date_ddmmyyyy=day)

    def _tax_setup_run(self) -> None:
        if not self._pricing_ready():
            return
        try:
            settings = self._tax_setup_settings()
        except ValueError as exc:
            messagebox.showerror(_TITLE, str(exc))
            return
        out_dir = Path(self.pricing_output.get().strip() or str(Path.home()))
        self.pricing_summary.configure(text="")
        self._pricing_start(self._tax_setup_worker, (settings, out_dir),
                            "Reading the tax screen…")

    def _tax_setup_worker(self, settings, out_dir: Path) -> None:
        from . import tax_setup as ts
        from . import tax_setup_report as tr
        from .pricing_gui import PRICING_MSG_ERROR, PRICING_MSG_LOG, _explain
        from .route_optimisation_report import writable_path
        try:
            res = ts.run_tax_setup_check(
                self._pricing_client(), settings, stop=self._pricing_stop,
                progress=lambda m: self._post(PRICING_MSG_LOG, m))
            out_dir.mkdir(parents=True, exist_ok=True)
            path = writable_path(out_dir / tr.default_filename())
            tr.build_workbook(res, path)
            self._post(PRICING_MSG_TAX_SETUP_DONE, (str(path), res))
        except InterruptedError:
            self._post(PRICING_MSG_ERROR, "Stopped — no workbook written.")
        except Exception as exc:                  # noqa: BLE001
            log.exception("Tax setup check failed")
            self._post(PRICING_MSG_ERROR, _explain(exc))

    def _tax_setup_handle_msg(self, kind: str, payload) -> bool:
        if kind != PRICING_MSG_TAX_SETUP_DONE:
            return False
        path, res = payload
        self._pricing_finish()
        self._pricing_last_path = path
        for b in self._pricing_buttons("open"):
            b.configure(state="normal")
        self._pricing_log(f"Done — saved {Path(path).name}.")
        self.pricing_summary.configure(text=tax_setup_summary(res))
        if res.notes:
            self.pricing_warning.configure(
                text=f"{len(res.notes)} note(s) — see the 'About' sheet.")
        return True


def tax_setup_summary(res) -> str:
    from .tax_setup import FINDING_ORDER
    counts = res.count()
    found = [f"{k}: {counts[k]:,}" for k in FINDING_ORDER if counts.get(k)]
    return (f"{len(res.routes):,} route(s) on {res.date} · "
            f"{res.searches:,} searches\n"
            + (" · ".join(found) if found else "Nothing looks wrong."))
