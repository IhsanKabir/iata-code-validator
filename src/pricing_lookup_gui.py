"""The "Look up" section of the Pricing tab: everything Zenith holds for some
fare bases or routes, for the seasons ticked -- no comparison.

Mixed into PricingMixin, which provides the fold-up section, the worker
plumbing (`_pricing_ready`, `_pricing_start`, `_pricing_client`,
`_pricing_index`, `_pricing_stop`) and the shared status lines.
"""
from __future__ import annotations

import json
import logging
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

log = logging.getLogger(__name__)

PRICING_MSG_LOOKUP_DONE = "pricing_lookup_done"   # payload: (path, result)
PRICING_MSG_SEASONS = "pricing_seasons"           # payload: [Season]

_TITLE = "Pricing lookup"
OWN_CURRENCY = "Each fare's own currency"


def qs_currency(choice: str) -> str:
    """The combobox's text -> '' (own currency) or a 3-letter code."""
    import re
    text = (choice or "").strip()
    if not text or text == OWN_CURRENCY:
        return ""
    if not re.fullmatch(r"[A-Za-z]{3}", text):
        raise ValueError(f"'{text}' is not a currency code — use 3 letters "
                         f"such as USD.")
    return text.upper()


class PricingLookupMixin:
    #: Shown on the section header's ⓘ.
    LOOKUP_HELP = (
                "Everything Zenith holds for the fare bases or routes you "
                "name, for the seasons you tick — no comparison:\n\n"
                "• Fare basis — its settings and rules (level, stays, "
                "one-way allowed, advance purchase, refundable…).\n"
                "• Fares — every Fare Editor row: currency, OW/RT, GDS, "
                "baggage, and who last changed it and when.\n"
                "• Penalties — change and refund fees per window and "
                "passenger type.\n"
                "• Taxes — every tax on the route, and who is exempt from it "
                "(your fare bases, passenger types, points of sale…).\n"
                "• Quick Sale — how each fare sells, with and without tax "
                "(current and later seasons only).\n\n"
                "Fare basis: codes, or patterns — YBD* is every code starting "
                "YBD. Route alone: every fare basis on it (from the route "
                "list). READ ONLY, like the season check.")

    def _pricing_build_lookup(self, parent: ttk.Frame) -> None:
        box = self._section(parent, "What to look up")
        self.lookup_fare_bases = tk.StringVar(value="")
        row = ttk.Frame(box)
        ttk.Entry(row, textvariable=self.lookup_fare_bases, width=50
                  ).pack(side="left")
        ttk.Label(row, text="  e.g. YBDCCUO, BBDQAR or YBD*",
                  style="Hint.TLabel").pack(side="left")
        self._form_row(box, 0, "Fare basis:", row, label_width=18)
        self.lookup_rbd = tk.StringVar(value="")
        row = ttk.Frame(box)
        ttk.Entry(row, textvariable=self.lookup_rbd, width=20
                  ).pack(side="left")
        ttk.Label(row, text="  e.g. E, S, V, K — alone, or to narrow a route "
                            "or fare basis pattern",
                  style="Hint.TLabel").pack(side="left")
        self._form_row(box, 1, "Booking class (RBD):", row, label_width=18)
        self.lookup_routes = tk.StringVar(value="")
        row = ttk.Frame(box)
        ttk.Entry(row, textvariable=self.lookup_routes, width=50
                  ).pack(side="left")
        ttk.Label(row, text="  e.g. DAC-DOH, DAC-DXB — commas, spaces or "
                            "one per line", style="Hint.TLabel"
                  ).pack(side="left")
        self._form_row(box, 2, "Routes:", row, label_width=18)
        self.lookup_return = tk.BooleanVar(value=True)
        self._form_row(box, 3, "", ttk.Checkbutton(
            box, text="Include the return direction",
            variable=self.lookup_return), label_width=18)
        self.lookup_season_row = ttk.Frame(box)
        self._form_row(box, 4, "Seasons:", self.lookup_season_row,
                       label_width=18)
        self.lookup_seasons: dict = {}          # season id -> BooleanVar
        self.lookup_parts = {p: tk.BooleanVar(value=True) for p in
                             ("Fare basis & fares", "Penalties", "Taxes",
                              "Quick Sale")}
        row = ttk.Frame(box)
        for p, var in self.lookup_parts.items():
            ttk.Checkbutton(row, text=p, variable=var).pack(side="left",
                                                            padx=(0, 10))
        self._form_row(box, 5, "Parts:", row, label_width=18)
        self.lookup_exemptions = tk.BooleanVar(value=True)
        self._form_row(box, 6, "", ttk.Checkbutton(
            box, text="Tax exemptions — opens each tax (about 3 s each, ~40 s "
                      "per route and season)",
            variable=self.lookup_exemptions), label_width=18)
        self.lookup_qs_currency = tk.StringVar(value=OWN_CURRENCY)
        row = ttk.Frame(box)
        ttk.Combobox(row, textvariable=self.lookup_qs_currency, width=28,
                     values=[OWN_CURRENCY, "BDT", "USD", "AED", "SAR", "QAR",
                             "OMR", "KWD", "MYR", "SGD", "INR", "CNY"]
                     ).pack(side="left")
        ttk.Label(row, text="  pick one, or type any 3-letter code",
                  style="Hint.TLabel").pack(side="left")
        self._form_row(box, 7, "Quick Sale currency:", row, label_width=18)

        ctl = ttk.Frame(parent)
        ctl.pack(fill="x", padx=4, pady=(8, 4))
        self.btn_lookup_run = ttk.Button(ctl, text="Look up",
                                         style="Primary.TButton",
                                         command=self._lookup_run)
        self.btn_lookup_run.pack(side="left")
        self.btn_lookup_seasons = ttk.Button(ctl, text="Refresh seasons",
                                             command=self._lookup_refresh)
        self.btn_lookup_seasons.pack(side="left", padx=(8, 0))
        self.btn_lookup_stop = ttk.Button(ctl, text="Stop", state="disabled",
                                          style="Danger.TButton",
                                          command=self._pricing_stop_clicked)
        self.btn_lookup_stop.pack(side="left", padx=(8, 0))
        self.btn_lookup_open = ttk.Button(ctl, text="Open workbook",
                                          state="disabled",
                                          command=self._pricing_open)
        self.btn_lookup_open.pack(side="left", padx=(8, 0))
        self._lookup_show_seasons(self._lookup_saved_seasons())

    # ---- seasons ----------------------------------------------------------
    @staticmethod
    def _lookup_seasons_file() -> Path:
        from . import config
        return Path(config.APP_DIR) / "pricing_seasons.json"

    def _lookup_saved_seasons(self) -> list:
        from .pricing_parse import Season
        try:
            raw = json.loads(self._lookup_seasons_file().read_text(
                encoding="utf-8"))
            return [Season(int(s["id"]), s["name"], s["start"], s["end"])
                    for s in raw]
        except (OSError, ValueError, KeyError, TypeError):
            return []

    def _lookup_show_seasons(self, seasons: list) -> None:
        from datetime import date

        from .pricing_check import current_season
        for w in self.lookup_season_row.winfo_children():
            w.destroy()
        before = {i for i, v in self.lookup_seasons.items() if v.get()}
        self.lookup_seasons = {}
        if not seasons:
            ttk.Label(self.lookup_season_row, text="Sign in, then press "
                      "'Refresh seasons'.", style="Hint.TLabel"
                      ).pack(side="left")
            return
        now = current_season(seasons, date.today())
        for s in seasons:
            ticked = s.id in before if before else (now is not None
                                                    and s.id == now.id)
            var = tk.BooleanVar(value=ticked)
            self.lookup_seasons[s.id] = var
            ttk.Checkbutton(self.lookup_season_row, text=s.name,
                            variable=var).pack(side="left", padx=(0, 10))

    def _lookup_refresh(self) -> None:
        if not self._pricing_ready():
            return

        def work() -> None:
            from . import pricing_lookup as pl
            try:
                seasons = pl.seasons_of(self._pricing_client())
                self._lookup_seasons_file().parent.mkdir(parents=True,
                                                         exist_ok=True)
                self._lookup_seasons_file().write_text(json.dumps(
                    [{"id": s.id, "name": s.name, "start": s.start,
                      "end": s.end} for s in seasons]), encoding="utf-8")
                self._post(PRICING_MSG_SEASONS, seasons)
            except Exception as exc:              # noqa: BLE001
                from .pricing_gui import PRICING_MSG_ERROR, _explain
                log.exception("Reading seasons failed")
                self._post(PRICING_MSG_ERROR, _explain(exc))

        self._pricing_start(work, (), "Reading the seasons from Zenith…")

    # ---- the lookup -------------------------------------------------------
    def _lookup_run(self) -> None:
        if not self._pricing_ready():
            return
        from .pricing_lookup import LookupSettings
        seasons = [i for i, v in self.lookup_seasons.items() if v.get()]
        parts = {p: v.get() for p, v in self.lookup_parts.items()}
        if not (self.lookup_fare_bases.get().strip()
                or self.lookup_routes.get().strip()
                or self.lookup_rbd.get().strip()):
            messagebox.showerror(_TITLE, "Enter a fare basis, a booking "
                                         "class or a route.")
            return
        try:
            currency = qs_currency(self.lookup_qs_currency.get())
        except ValueError as exc:
            messagebox.showerror(_TITLE, str(exc))
            return
        if not seasons:
            messagebox.showerror(_TITLE, "Tick at least one season (press "
                                         "'Refresh seasons' if none show).")
            return
        if not any(parts.values()):
            messagebox.showerror(_TITLE, "Tick at least one part.")
            return
        settings = LookupSettings(
            fare_bases_text=self.lookup_fare_bases.get(),
            rbd_text=self.lookup_rbd.get(),
            routes_text=self.lookup_routes.get(),
            include_return=bool(self.lookup_return.get()),
            season_ids=seasons, fares=parts["Fare basis & fares"],
            penalties=parts["Penalties"], taxes=parts["Taxes"],
            tax_exemptions=bool(self.lookup_exemptions.get()),
            quick_sale=parts["Quick Sale"], qs_currency=currency)
        out_dir = Path(self.pricing_output.get().strip() or str(Path.home()))
        self.pricing_summary.configure(text="")
        self._pricing_start(self._lookup_worker, (settings, out_dir),
                            "Looking up…")

    def _lookup_worker(self, settings, out_dir: Path) -> None:
        from . import pricing_lookup as pl
        from . import pricing_lookup_report as plr
        from .pricing_gui import PRICING_MSG_ERROR, PRICING_MSG_LOG, _explain
        from .route_optimisation_report import writable_path
        try:
            res = pl.run_lookup(self._pricing_client(), self._pricing_index(),
                                settings, stop=self._pricing_stop,
                                progress=lambda m: self._post(PRICING_MSG_LOG,
                                                              m))
            out_dir.mkdir(parents=True, exist_ok=True)
            path = writable_path(out_dir / plr.default_filename())
            plr.build_workbook(res, path)
            self._post(PRICING_MSG_LOOKUP_DONE, (str(path), res))
        except InterruptedError:
            self._post(PRICING_MSG_ERROR, "Stopped — no workbook written.")
        except Exception as exc:                  # noqa: BLE001
            log.exception("Pricing lookup failed")
            self._post(PRICING_MSG_ERROR, _explain(exc))

    def _lookup_handle_msg(self, kind: str, payload) -> bool:
        if kind == PRICING_MSG_SEASONS:
            self._pricing_finish()
            self._lookup_show_seasons(payload)
            self._pricing_log(f"{len(payload)} seasons read from Zenith.")
        elif kind == PRICING_MSG_LOOKUP_DONE:
            path, res = payload
            self._pricing_finish()
            self._pricing_last_path = path
            for b in self._pricing_buttons("open"):
                b.configure(state="normal")
            self._pricing_log(f"Done — saved {Path(path).name}.")
            self.pricing_summary.configure(text=lookup_summary(res))
            if res.notes:
                self.pricing_warning.configure(
                    text=f"{len(res.notes)} note(s) — see the 'About' sheet.")
        else:
            return False
        return True


def lookup_summary(res) -> str:
    return (f"{len(res.fare_bases):,} fare bases · {len(res.routes):,} "
            f"route(s) · {', '.join(res.seasons)}\n"
            f"Fares {len(res.fares):,} rows · Penalties "
            f"{len(res.penalties):,} · Taxes {len(res.taxes):,} · Quick Sale "
            f"{len(res.quick_sale):,}")
