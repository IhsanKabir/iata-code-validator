"""The tax setup check workbook.

* About      what was checked, on which date, and how many of each finding
* Findings   one row per thing that looks wrong, most important first
* Coverage   one row per route, one column per tax code -- the amount set,
             "—" where none is (red when that is a finding)
* All taxes  every tax entry found, per route it applies to
"""
from __future__ import annotations

from datetime import date

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from . import pricing_parse as pp
from . import tax_setup as ts
from .counter_master import BAD, GREY, NAVY, PAPER, WARN, _cell, _headers
from .pricing_lookup_report import write_table

#: Findings that usually need fixing get red, the rest amber / none.
_RED = {ts.NOT_READ, ts.NOT_SET_UP, ts.MISSING, ts.DIFFERENT, ts.TWICE}
_AMBER = {ts.SOME, ts.NOT_APPLICABLE, ts.NOT_TICKED}

_HELP = {
    ts.NOT_SET_UP: "A code you asked for is not on the route, though other "
                   "routes from the same airport have it.",
    ts.MISSING: "Most routes from this airport (same route type) carry this "
                "tax; this one does not.",
    ts.DIFFERENT: "The same tax from the same airport has different amounts "
                  "on different routes.",
    ts.TWICE: "The same tax is set up twice on one route.",
    ts.SOME: "Only a few routes from this airport carry this tax.",
    ts.NOT_APPLICABLE: "Set up, but Zenith marks the entry not applicable.",
    ts.NOT_TICKED: "Named for the route, but its International / Domestic "
                   "tick does not fit the route.",
    ts.ZERO: "Applicable, with a zero amount.",
    ts.NOT_AT_AIRPORT: "A code you asked for that no checked route from this "
                       "airport has — often expected (e.g. a UAE departure "
                       "tax on routes leaving Bangkok).",
    ts.NO_ROUTE_TAX: "No tax named for this route — not sold, or not set up?",
    ts.NOT_READ: "A tax on this route's search could not be placed on a "
                 "route (see Notes), so the route is not judged for gaps.",
}


def coverage(res) -> tuple:
    """(codes, rows): one dict per route, the amounts per tax code."""
    codes = list(res.codes_asked) or sorted(
        {r.code for rows in res.per_route.values() for r in rows})
    rows = []
    for dep, arr in res.routes:
        row = {"Route": f"{dep}-{arr}", "Route type": ts.route_kind(dep, arr)}
        for code in codes:
            shown = []
            for r in res.per_route.get((dep, arr), []):
                if r.code != code:
                    continue
                text = r.value + ("*" if r.every_route else "")
                shown.append(text if r.applicable
                             else f"(not applicable: {text})")
            row[code] = " + ".join(shown) or "—"
        rows.append(row)
    return codes, rows


def _gaps(res) -> set:
    """(route, code) pairs a finding says are missing."""
    return {(f.route, f.code) for f in res.findings
            if f.kind in (ts.NOT_SET_UP, ts.MISSING)}


def write_coverage(ws, res) -> None:
    ws.sheet_view.showGridLines = False
    _cell(ws, 1, 1, "Coverage", bold=True, size=15, color=NAVY)
    _cell(ws, 2, 1, "Amount set per route and tax code on "
                    f"{res.date}. * = set for every route from (or to) the "
                    "airport. — = none; red when that is a finding.",
          size=9, color=GREY)
    codes, rows = coverage(res)
    if not rows:
        _cell(ws, 4, 1, "No routes checked.", fill=PAPER)
        return
    cols = ["Route", "Route type", *codes]
    _headers(ws, 4, cols)
    gaps = _gaps(res)
    for n, row in enumerate(rows, start=5):
        for i, c in enumerate(cols, start=1):
            v = row[c]
            fill = None
            if c in codes and (row["Route"], c) in gaps:
                fill = BAD
            elif c in codes and v.startswith("(not applicable"):
                fill = WARN
            _cell(ws, n, i, v, border=True, fill=fill)
    ws.column_dimensions["A"].width = 11
    ws.column_dimensions["B"].width = 11
    for i, c in enumerate(codes, start=3):
        width = max([len(c), *(len(r[c]) for r in rows[:400])])
        ws.column_dimensions[get_column_letter(i)].width = min(width + 2, 40)
    ws.auto_filter.ref = f"A4:{get_column_letter(len(cols))}{len(rows) + 4}"
    ws.freeze_panes = "C5"


def all_rows(res) -> list:
    out = []
    for (dep, arr), rows in sorted(res.per_route.items()):
        for r in rows:
            out.append({
                "Route": f"{dep}-{arr}", "Route type": ts.route_kind(dep, arr),
                "Tax code": r.code, "Tax": r.name, "Value": r.value,
                "Set for": (f"every route to {r.arr}" if r.dep == ts.ANY
                            else f"every route from {r.dep}"
                            if r.arr == ts.ANY else "this route"),
                "International": pp.tick_text(r.international, r.crossed),
                "Domestic": "yes" if r.domestic else "no",
                "Applicable": "yes" if r.applicable else "NO",
                "Tax id": r.tax_id})
    return out


def build_workbook(res, out_path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "About"
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 100
    _cell(ws, 1, 1, "Tax setup check — read only", bold=True, size=15,
          color=NAVY)
    lines = [("Run", f"{res.started} to {res.finished}"),
             ("Travel date", res.date),
             ("Route types", ", ".join(res.kinds)),
             ("Tax codes", ", ".join(res.codes_asked) or "every tax found"),
             ("Routes", f"{len(res.routes):,} ({res.searches:,} airport-pair "
                        f"searches)")]
    r = 3
    for label, value in lines:
        _cell(ws, r, 1, label, bold=True)
        _cell(ws, r, 2, value, wrap=True)
        r += 1
    r += 1
    _cell(ws, r, 1, "Findings", bold=True)
    counts = res.count()
    for kind in ts.FINDING_ORDER:
        r += 1
        _cell(ws, r, 1, f"{kind}: {counts.get(kind, 0):,}",
              fill=_fill(kind) if counts.get(kind) else None)
        _cell(ws, r, 2, _HELP[kind], wrap=True)
    r += 2
    _cell(ws, r, 1, "Notes", bold=True)
    for note in res.notes or ["None — every search was read."]:
        _cell(ws, r, 2, note, wrap=True)
        r += 1
    findings = wb.create_sheet("Findings")
    write_table(findings, "Findings", "What looks wrong, most important "
                "first. Filter on Kind.", [f.row() for f in res.findings])
    for n, f in enumerate(res.findings, start=5):
        if _fill(f.kind):
            findings.cell(row=n, column=1).fill = _pattern(_fill(f.kind))
    write_coverage(wb.create_sheet("Coverage"), res)
    write_table(wb.create_sheet("All taxes"), "All taxes", "Every tax entry "
                "found, on each route it applies to.", all_rows(res))
    wb.save(str(out_path))


def _fill(kind: str):
    return BAD if kind in _RED else WARN if kind in _AMBER else None


def _pattern(color: str):
    from openpyxl.styles import PatternFill
    return PatternFill("solid", fgColor=color)


def default_filename() -> str:
    return f"Tax_Setup_Check_{date.today():%Y-%m-%d}.xlsx"
