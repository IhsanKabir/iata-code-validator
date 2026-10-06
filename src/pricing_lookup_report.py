"""The pricing lookup workbook: one sheet per part, one row per season.

Plain tables with filters, so a season, a route or a passenger type is one
click away in Excel. Columns that are empty on every row are left out.
"""
from __future__ import annotations

from datetime import date

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from .counter_master import GREY, NAVY, PAPER, _cell, _headers
from .pricing_report import LABELS

SHEETS = (("Fare basis", "fare_bases",
           "The fare basis's own settings, as Zenith stores them (codes "
           "such as Refundable or Combinability are Zenith's own numbers)."),
          ("Fares", "fares", "Every Fare Editor row, per season — with who "
                             "last changed it and when."),
          ("Penalties", "penalties", "Change and refund fees per time window "
                                     "and passenger type."),
          ("Taxes", "taxes", "Every tax on the route on a day in the season, "
                             "and what is exempt from it."),
          ("Quick Sale", "quick_sale", "How each fare sells: amounts without "
                                       "tax, and the tax (with-tax minus "
                                       "without), in the display currency."))


def _columns(rows: list) -> list:
    cols: list = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    return [c for c in cols
            if any(r.get(c) not in (None, "", 0, 0.0, False) for r in rows)
            or c in ("Season", "Route", "Fare basis", "Code")]


def write_table(ws, title: str, note: str, rows: list) -> None:
    ws.sheet_view.showGridLines = False
    _cell(ws, 1, 1, title, bold=True, size=15, color=NAVY)
    _cell(ws, 2, 1, note, size=9, color=GREY)
    if not rows:
        ws.column_dimensions["A"].width = 60
        _cell(ws, 4, 1, "Nothing to show — not asked for, or nothing found.",
              fill=PAPER)
        return
    cols = _columns(rows)
    _headers(ws, 4, [LABELS.get(c, c) for c in cols])
    for i, c in enumerate(cols, start=1):
        width = max(len(str(LABELS.get(c, c))),
                    *(len(str(r.get(c) if r.get(c) is not None else ""))
                      for r in rows[:300]))
        ws.column_dimensions[get_column_letter(i)].width = min(max(width + 2,
                                                                   9), 48)
    for n, r in enumerate(rows, start=5):
        for i, c in enumerate(cols, start=1):
            v = r.get(c)
            _cell(ws, n, i, "" if v is None else v, border=True)
    ws.auto_filter.ref = f"A4:{get_column_letter(len(cols))}{len(rows) + 4}"
    ws.freeze_panes = "C5"


def build_workbook(res, out_path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "About"
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 100
    _cell(ws, 1, 1, "Pricing lookup — read only", bold=True, size=15,
          color=NAVY)
    lines = [("Run", f"{res.started} to {res.finished}"),
             ("Seasons", ", ".join(res.seasons)),
             ("Routes", ", ".join(res.routes) or "—"),
             ("Fare bases", ", ".join(str(f.get("Code"))
                                      for f in res.fare_bases))]
    r = 3
    for label, value in lines:
        _cell(ws, r, 1, label, bold=True)
        _cell(ws, r, 2, value, wrap=True)
        r += 1
    r += 1
    _cell(ws, r, 1, "Notes", bold=True)
    for note in res.notes or ["None — everything asked for was read."]:
        _cell(ws, r, 2, note, wrap=True)
        r += 1
    for title, attr, note in SHEETS:
        write_table(wb.create_sheet(title), title, note, getattr(res, attr))
    wb.save(str(out_path))


def default_filename() -> str:
    return f"Pricing_Lookup_{date.today():%Y-%m-%d}.xlsx"
