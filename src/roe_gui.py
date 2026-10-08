"""The "Exchange rates (ROE)" section of the Pricing tab.

Preview reads the day's rate sheet and Zenith's ROE screen and shows what
would change; Save writes only that, after a confirmation, then checks it
five ways (see roe.apply). Mixed into PricingMixin, which provides the
fold-up section, the worker plumbing and the shared status lines.
"""
from __future__ import annotations

import logging
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

log = logging.getLogger(__name__)

PRICING_MSG_ROE_PLAN = "pricing_roe_plan"     # payload: RoePlan
PRICING_MSG_ROE_DONE = "pricing_roe_done"     # payload: (path, plan, result)

_TITLE = "Exchange rates (ROE)"
#: A preview older than this is read again before saving is allowed.
PREVIEW_FRESH_S = 10 * 60

ROE_HELP = (
    "Updates Zenith's exchange rates (ROE) from the day's rate sheet — the "
    "Excel your Travelport automation produces ('1 KWD = 401.00799' BDT).\n\n"
    "Zenith keeps 1 BDT = x KWD with 6 decimals; the sheet's rate is turned "
    "round and CUT to 6 decimals, exactly as the team enters it by hand.\n\n"
    "1. Preview — reads the sheet and Zenith and lists what would change. "
    "Nothing is saved.\n"
    "2. Save to Zenith — after you confirm, saves only those rates, page by "
    "page, as the screen's own Save does. If a rate moved since the "
    "preview, it stops.\n"
    "3. Checks — Zenith's reply, a fresh read of every page (every other "
    "currency untouched), 'Last modified', the rate history, and the rate "
    "Quick Sale converts with. A record workbook is saved.")


def latest_sheet(folder: Path) -> str:
    """The newest '…ROE…' / '…ROW…' workbook in a folder, or ''."""
    try:
        found = [p for p in folder.glob("*.xlsx")
                 if not p.name.startswith("~$")
                 and any(w in p.stem.upper() for w in ("ROE", "ROW"))]
    except OSError:
        return ""
    return str(max(found, key=lambda p: p.stat().st_mtime)) if found else ""


class RoeMixin:
    def _roe_build(self, parent: ttk.Frame) -> None:
        self._roe_plan = None
        box = self._section(parent, "Today's rates")
        self.roe_sheet = tk.StringVar(
            value=latest_sheet(Path.home() / "Downloads"))
        self._form_row(box, 0, "Rate sheet:",
                       ttk.Entry(box, textvariable=self.roe_sheet),
                       label_width=18,
                       suffix=ttk.Button(box, text="Browse…",
                                         command=self._roe_pick))
        self.roe_linked = tk.BooleanVar(value=True)
        self._form_row(box, 1, "", ttk.Checkbutton(
            box, text="Also update the taxes linked to a changed rate "
                      "(e.g. E5, T6 on USD) — Zenith's 'Yes'",
            variable=self.roe_linked), label_width=18)

        ctl = ttk.Frame(parent)
        ctl.pack(fill="x", padx=4, pady=(8, 4))
        self.btn_roe_preview = ttk.Button(ctl, text="Preview",
                                          style="Primary.TButton",
                                          command=self._roe_preview)
        self.btn_roe_preview.pack(side="left")
        self.btn_roe_save = ttk.Button(ctl, text="Save to Zenith",
                                       style="Danger.TButton",
                                       state="disabled",
                                       command=self._roe_save)
        self.btn_roe_save.pack(side="left", padx=(8, 0))
        self.btn_roe_open = ttk.Button(ctl, text="Open record",
                                       state="disabled",
                                       command=self._pricing_open)
        self.btn_roe_open.pack(side="left", padx=(8, 0))

        cols = (("code", "Currency", 80), ("sheet", "Sheet: 1 XXX = BDT", 140),
                ("now", "Zenith now", 100), ("new", "New rate", 100),
                ("change", "Change", 80), ("note", "Notes / checks", 520))
        self.roe_tree = ttk.Treeview(parent, columns=[c[0] for c in cols],
                                     show="headings", height=12)
        for cid, label, width in cols:
            self.roe_tree.heading(cid, text=label)
            self.roe_tree.column(cid, width=width,
                                 anchor="w" if cid in ("code", "note")
                                 else "e")
        self.roe_tree.tag_configure("bad", foreground="#B00020")
        self.roe_tree.tag_configure("good", foreground="#107C10")
        self.roe_tree.pack(fill="x", padx=4, pady=4)
        self.roe_notes = ttk.Label(parent, text="", style="Hint.TLabel",
                                   wraplength=900, justify="left")
        self.roe_notes.pack(anchor="w", padx=4)

    def _roe_pick(self) -> None:
        got = filedialog.askopenfilename(
            title="Today's rate sheet", filetypes=[("Excel", "*.xlsx")],
            initialdir=str(Path(self.roe_sheet.get()).parent)
            if self.roe_sheet.get() else str(Path.home() / "Downloads"))
        if got:
            self.roe_sheet.set(got)

    # ---- preview ----------------------------------------------------------
    def _roe_preview(self) -> None:
        if not self._pricing_ready():
            return
        path = Path(self.roe_sheet.get().strip())
        if not path.is_file():
            messagebox.showerror(_TITLE, "Pick today's rate sheet first.")
            return
        self._roe_plan = None
        self.btn_roe_save.configure(state="disabled")

        def work() -> None:
            from datetime import date

            from . import roe
            from .pricing_gui import PRICING_MSG_ERROR, _explain
            try:
                sheet = roe.read_rate_sheet(path)
                pages = roe.read_all_pages(self._pricing_client())
                self._post(PRICING_MSG_ROE_PLAN, roe.make_plan(
                    sheet, pages, today=date.today()))
            except Exception as exc:              # noqa: BLE001
                log.exception("ROE preview failed")
                self._post(PRICING_MSG_ERROR, _explain(exc))

        self._pricing_start(work, (), "Reading the sheet and Zenith's ROE "
                                      "screen…")

    def _roe_show_plan(self, plan) -> None:
        from .roe_report import preview_rows
        t = self.roe_tree
        t.delete(*t.get_children())
        for row in preview_rows(plan):
            alarm = ("BIG" in row[5] or "NOT saved" in row[5]
                     or "not a currency" in row[5])
            tags = ("bad",) if alarm else ()
            t.insert("", "end", values=row, tags=tags)
        n = len(plan.changes)
        self.roe_notes.configure(text="\n".join(plan.warnings))
        self._pricing_log(
            f"{n} rate(s) to change, {len(plan.same)} already right — "
            "check the list, then press Save to Zenith." if n
            else "Nothing to change — Zenith already has the sheet's rates.")
        self.btn_roe_save.configure(state="normal" if n else "disabled")

    # ---- save -------------------------------------------------------------
    def _roe_save(self) -> None:
        plan = self._roe_plan
        if plan is None or not plan.changes:
            messagebox.showinfo(_TITLE, "Press Preview first.")
            return
        if time.time() - plan.read_at > PREVIEW_FRESH_S:
            messagebox.showinfo(_TITLE, "The preview is over 10 minutes old "
                                        "— press Preview again.")
            return
        if not self._pricing_ready():
            return
        from .roe import rate_text
        lines = [f"{c.code}: {rate_text(c.old)} → {rate_text(c.new)} "
                 f"({c.pct:+.2f}%)" + ("  ⚠ big change" if c.big else "")
                 for c in plan.changes]
        linked = plan.linked_taxes()
        extra = ""
        if linked:
            extra = ("\n\nLinked taxes " + ", ".join(linked) + " will "
                     + ("ALSO be updated." if self.roe_linked.get()
                        else "NOT be updated."))
        if plan.warnings:
            extra += "\n\nNotes:\n" + "\n".join(plan.warnings)
        if not messagebox.askyesno(
                _TITLE, f"Save {len(lines)} exchange rate(s) to Zenith?\n\n"
                        + "\n".join(lines) + extra, icon="warning"):
            return
        update_linked = bool(self.roe_linked.get())
        out_dir = Path(self.pricing_output.get().strip() or str(Path.home()))
        self.btn_roe_save.configure(state="disabled")

        def work() -> None:
            from . import roe
            from . import roe_report as rr
            from .pricing_gui import PRICING_MSG_ERROR, PRICING_MSG_LOG
            from .route_optimisation_report import writable_path
            try:
                res = roe.apply(self._pricing_client(), plan,
                                update_linked=update_linked,
                                progress=lambda m: self._post(
                                    PRICING_MSG_LOG, m))
                out_dir.mkdir(parents=True, exist_ok=True)
                path = writable_path(out_dir / rr.default_filename())
                rr.build_workbook(plan, res, path)
                self._post(PRICING_MSG_ROE_DONE, (str(path), plan, res))
            except Exception as exc:              # noqa: BLE001
                log.exception("ROE save failed")
                self._post(PRICING_MSG_ERROR,
                           f"{type(exc).__name__}: {exc} — open Zenith's ROE "
                           f"screen and check the rates before retrying.")

        self._roe_plan = None                     # a plan is saved once
        self._pricing_start(work, (), "Saving exchange rates…")

    def _roe_show_result(self, path: str, plan, res) -> None:
        t = self.roe_tree
        t.delete(*t.get_children())
        by_code = {c.code: c for c in plan.changes}
        for chk in res.checks:
            c = by_code[chk.code]
            note = " · ".join(f"{n} {v}" for n, v in (
                ("reply", chk.saved), ("read", chk.reread),
                ("modified", chk.modified), ("history", chk.history),
                ("QS", chk.quick_sale)))
            t.insert("", "end", values=(chk.code, f"{c.bdt_per_unit:f}",
                                        chk.old, chk.new, f"{c.pct:+.2f}%",
                                        note),
                     tags=("good",) if chk.ok else ("bad",))
        self.roe_notes.configure(text="Every other currency: " + (
            "unchanged ✓" if res.untouched_ok
            else " · ".join(res.untouched_notes)))
        ok = not res.failed and res.untouched_ok and all(c.ok for c in
                                                         res.checks)
        self._pricing_log(("Saved and checked ✓ — " if ok else
                           "Saved with problems — ") + Path(path).name)
        if res.failed:
            self.pricing_warning.configure(text=res.failed)

    def _roe_handle_msg(self, kind: str, payload) -> bool:
        if kind == PRICING_MSG_ROE_PLAN:
            self._pricing_finish()
            self._roe_plan = payload
            self._roe_show_plan(payload)
        elif kind == PRICING_MSG_ROE_DONE:
            path, plan, res = payload
            self._pricing_finish()
            self._pricing_last_path = path
            for b in self._pricing_buttons("open"):
                b.configure(state="normal")
            self._roe_show_result(path, plan, res)
        else:
            return False
        return True
