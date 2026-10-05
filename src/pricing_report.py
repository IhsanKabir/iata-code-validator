"""The season check workbook.

For each part two sheets: the VALUES -- everything read, every season side
by side, each value that differs from the current season highlighted --
and the DIFFERENCES, one line each. Plus:
  Summary       which seasons, which routes, how much was compared, how
                many differences each part found
  Not checked   anything the check could not read -- never silently dropped

An empty sheet says so in words: "no differences" must be visibly a
result, not a blank page that might be a failure.
"""
from __future__ import annotations

from datetime import date

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from .counter_master import BAD, GOOD, GREY, NAVY, PAPER, WARN, _cell, _headers

PARTS = ("Fares", "Sales check", "Quick Sale", "Taxes", "Penalties")
HEADERS = ("Route", "Item", "Field / window", "Finding", "Season A",
           "Value A", "Season B", "Value B")
WIDTHS = (26, 44, 30, 20, 18, 22, 18, 22)
_TONE = {"different": WARN, "amount differs": WARN, "missing": BAD,
         "not on sale": BAD, "new": PAPER, "duplicate": BAD}

#: Short column names for Fare Editor fields.
LABELS = {
    "AmountOW": "OW", "AmountRT": "RT", "AmountYQ": "YQ", "AmountYR": "YR",
    "GdsAmountOW": "GDS OW", "GdsAmountRT": "GDS RT", "GdsAmountYQ": "GDS YQ",
    "GdsAmountYR": "GDS YR", "ID_BagConceptType": "Bag type",
    "BaggageWeightAllowance": "Bag kg",
    "BaggageExcessWeightAmount": "Excess /kg",
    "BagPieceAllowance": "Pieces", "BagWeightAllowancePerPiece": "Kg /piece",
    "BagPieceExcedentAmount": "Extra piece", "TravelerCheckTypeId":
    "Check type", "TravelerCheckAmountOW": "Check OW",
    "TravelerCheckAmountRT": "Check RT", "MilesAmountOW": "Miles OW",
    "MilesAmountRT": "Miles RT"}

_NOTES = {
    "Fares": "Fare Editor rows (fare basis × route × passenger type); every "
             "field compared, currency included.",
    "Sales check": "Adult fares as set in the Fare Editor against Quick "
                   "Sale's display in the same currency, per season. "
                   "'OW not sold' = Quick Sale sells the fare return only.",
    "Quick Sale": "Every fare Quick Sale lists (all levels), without taxes; "
                  "tax = with-tax price minus this. Amounts in the display "
                  "currency.",
    "Taxes": "The tax screen's list for each route on a day in each season "
             "— codes, values, YQ/YR.",
    "Penalties": "Change and refund fees per fare class, route, passenger "
                 "type and time window.",
}


def _widths(ws, widths) -> None:
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.sheet_view.showGridLines = False


def _title(ws, text: str, note: str) -> int:
    _cell(ws, 1, 1, text, bold=True, size=15, color=NAVY)
    _cell(ws, 2, 1, note, size=9, color=GREY)
    return 4


def _norm(field_name, value):
    from .pricing_check import _norm as norm
    return norm(field_name, value)


def _num(value):
    """Numbers stay numbers in Excel; empties show as a dash."""
    if value is None or value == "":
        return "—"
    return value


# ---- summary -----------------------------------------------------------------

def write_summary(ws, res) -> None:
    _widths(ws, (28, 16, 16, 16, 16, 16, 16))
    r = _title(ws, "Season check — read only",
               f"Run {res.started} to {res.finished}. Nothing in Zenith was "
               f"changed: only searches and displays were read.")
    for label, value in (("Base season", res.base),
                         ("Compared with", ", ".join(res.others)),
                         ("Routes", f"{len(res.routes):,}: "
                                    + ", ".join(res.routes[:40])
                                    + (" …" if len(res.routes) > 40 else ""))):
        _cell(ws, r, 1, label, bold=True)
        _cell(ws, r, 2, value)
        r += 1
    r += 1
    r = _headers(ws, r, ("Part", "Compared", "Differences", "Missing",
                         "New", "Other", "Result"))
    for part in PARTS:
        counts = res.count(part)
        diff = counts.get("different", 0) + counts.get("amount differs", 0)
        missing = counts.get("missing", 0) + counts.get("not on sale", 0)
        new = counts.get("new", 0)
        other = sum(counts.values()) - diff - missing - new
        compared = res.compared.get(part, 0)
        total = sum(counts.values())
        verdict = ("not run" if not compared and not total else
                   "all match" if not total else f"{total:,} to look at")
        tone = PAPER if verdict == "not run" else GOOD if not total else BAD
        for j, v in enumerate((part, compared, diff, missing, new, other,
                               verdict), start=1):
            _cell(ws, r, j, v, fill=tone if j == 7 else None, border=True,
                  fmt="#,##0" if isinstance(v, int) else None)
        r += 1
    r += 1
    if res.problems:
        _cell(ws, r, 1, f"{len(res.problems):,} item(s) could not be "
                        f"checked — see 'Not checked'.", bold=True,
              color="B00020")
    else:
        _cell(ws, r, 1, "Everything asked for was read.", color=GREY)


# ---- values: every season side by side ---------------------------------------

def _shown_fields(part, res, rows) -> list:
    """Fields worth a column: any value that is not empty or zero."""
    fields = res.value_fields.get(part, ())
    if part == "Sales check":
        return [f for f in fields if f != "Status"]
    return [f for f in fields
            if any(_norm(f, v.get(f)) not in (0.0, "")
                   for per in rows.values() for v in per.values())]


def _status(part, per: dict, seasons, fields) -> tuple:
    """(text, fill) for one row across the seasons."""
    if part == "Sales check":
        notes = sorted({v["Status"] for v in per.values()})
        bad = [n for n in notes if "differs" in n or "not in" in n]
        return ", ".join(notes), BAD if bad else GOOD
    missing = [s for s in seasons if s not in per]
    if missing:
        return "missing in " + ", ".join(missing), BAD
    base = per[seasons[0]]
    changed = [LABELS.get(f, f) for f in fields
               if any(_norm(f, per[s].get(f)) != _norm(f, base.get(f))
                      for s in seasons[1:])]
    return (("differs: " + ", ".join(changed), WARN) if changed
            else ("same", GOOD))


def write_values(ws, res, part: str) -> int:
    rows = res.values.get(part, {})
    seasons = res.seasons
    r = _title(ws, f"{part} — all values", _NOTES[part])
    if not rows:
        _widths(ws, (60,))
        _cell(ws, r, 1, "This part was not run, or nothing was read.",
              fill=PAPER)
        return 0
    fields = _shown_fields(part, res, rows)
    heads = ["Route", "Item", "Status"] + [
        f"{LABELS.get(f, f)}\n{s}" for f in fields for s in seasons]
    _widths(ws, [22, 34, 26] + [12] * (len(heads) - 3))
    top = r
    r = _headers(ws, r, heads)
    for (route, item), per in sorted(rows.items()):
        text, tone = _status(part, per, seasons, fields)
        _cell(ws, r, 1, route, border=True)
        _cell(ws, r, 2, item, border=True)
        _cell(ws, r, 3, text, border=True, fill=tone)
        c = 4
        for f in fields:
            base = per.get(seasons[0], {}).get(f)
            for s in seasons:
                v = per.get(s)
                if v is None:
                    _cell(ws, r, c, "—", border=True, fill=BAD)
                else:
                    differs = (part != "Sales check" and s != seasons[0]
                               and _norm(f, v.get(f)) != _norm(f, base))
                    _cell(ws, r, c, _num(v.get(f)), border=True,
                          fill=WARN if differs else None)
                c += 1
        r += 1
    ws.auto_filter.ref = f"A{top}:{get_column_letter(len(heads))}{r - 1}"
    ws.freeze_panes = f"D{top + 1}"
    return len(rows)


# ---- differences: one line each ----------------------------------------------

def write_part(ws, res, part: str) -> int:
    _widths(ws, WIDTHS)
    rows = [f for f in res.findings if f.part == part]
    r = _title(ws, f"{part} — differences", _NOTES[part])
    if not rows:
        compared = res.compared.get(part, 0)
        _cell(ws, r, 1, f"No differences — {compared:,} compared."
              if compared else "This part was not run.",
              bold=True, fill=GOOD if compared else PAPER)
        return 0
    start = r
    r = _headers(ws, r, HEADERS)
    for f in sorted(rows, key=lambda f: (f.route, f.item, f.detail)):
        for j, v in enumerate((f.route, f.item, f.detail, f.kind, f.season_a,
                               f.value_a, f.season_b, f.value_b), start=1):
            _cell(ws, r, j, v, border=True,
                  fill=_TONE.get(f.kind) if j == 4 else None)
        r += 1
    ws.auto_filter.ref = f"A{start}:H{r - 1}"
    ws.freeze_panes = f"A{start + 1}"
    return len(rows)


def write_problems(ws, res) -> None:
    _widths(ws, (120,))
    r = _title(ws, "Not checked", "What could not be read. These are NOT "
                                  "passes — check them by hand or re-run.")
    if not res.problems:
        _cell(ws, r, 1, "Nothing — every item asked for was read.",
              fill=GOOD)
        return
    for p in res.problems:
        _cell(ws, r, 1, p, wrap=True)
        r += 1


def build_workbook(res, out_path) -> None:
    wb = Workbook()
    wb.active.title = "Summary"
    write_summary(wb["Summary"], res)
    for part in PARTS:
        if not res.values.get(part) and not res.compared.get(part):
            continue                           # part not run: no sheets
        write_values(wb.create_sheet(part), res, part)
        write_part(wb.create_sheet(f"{part} - differences"), res, part)
    write_problems(wb.create_sheet("Not checked"), res)
    wb.save(str(out_path))


def default_filename() -> str:
    return f"Season_Check_{date.today():%Y-%m-%d}.xlsx"
