"""The Route Optimisation workbook.

Sheets:
  Summary        what was compared, and the routes worth acting on
  Every route    one block per city pair -- both directions combined, then
                 outbound and inbound -- ranked by the pair's seat share
  Who flies what one row per competitor per route, with the aircraft

The Summary carries the caveats rather than leaving them in a docstring,
because the numbers invite a confidence they do not deserve: competitor
frequency is what is on sale rather than what is scheduled, their seat
counts are published configurations rather than filed capacity, and a load
factor built on seats sold reads slightly above one built on seats flown.
"""
from __future__ import annotations

from openpyxl import Workbook

from . import route_optimisation as ro
from .counter_master import (BAD, GOOD, GREY, LAST, MONEY, NAVY, PAPER, PCT,
                             WARN, _band, _cell, _headers, _kpi_strip)

WIDTHS = [14, 8, 10, 11, 11, 10, 9, 10, 9, 10, 13, 9, 11, 10, 10,
          15, 12, 4, 4, 4, 30]

ROUTE_HEADERS = [
    "Route", "Load", "Our seats/day", "Our flights/day", "Rival seats/day",
    "Rival flights", "Seat share", "Flight share", "Aircraft", "Distance km",
    "Base fare/month", "RASK", "Top rival", "Their seats", "Their aircraft",
    "Base fare, RASK months", "Seats flown, RASK months", "", "", "",
    "Verdict"]

#: The RASK cells. RASK is a live formula over these, so a distance typed
#: into a blank Distance cell computes it on the spot.
DIST, RASK, FARE, SEATS = "J", "L", "P", "Q"
ROUTES_SHEET = "Every route"
KM = '#,##0;-#,##0;""'


def _formula(ws, r: int, col: str, formula: str, *, fmt=None, fill=None,
             bold=False) -> None:
    """A deliberate live formula. `_cell` refuses anything starting with
    '=' so that data can never become one; this is the one door in."""
    c = ws[f"{col}{r}"]
    _cell(ws, r, c.column, None, size=9, border=True, align="right",
          fill=fill, fmt=fmt, bold=bold)
    c.value = formula


def _rask_formula(parts) -> str:
    """RASK over the directions whose distance is filled in.

    `parts` are row numbers. A direction with no distance adds nothing to
    either side, so a pair shows RASK for the half it can -- and the whole
    the moment the other distance is typed in.
    """
    num = "+".join(f"IF(N({DIST}{x})>0,N({FARE}{x}),0)" for x in parts)
    den = "+".join(f"IF(N({DIST}{x})>0,N({SEATS}{x})*{DIST}{x},0)"
                   for x in parts)
    return f'=IFERROR(ROUND(({num})/({den}),2),"")'


def _direction_rask(ws, r: int, view, dist_from: str | None) -> None:
    """Distance, the two RASK inputs, and RASK itself, for one direction.

    `dist_from` is a formula supplying the distance from elsewhere (the
    reverse direction, or the Every route sheet); otherwise the table's
    distance is written, or the cell is left blank and shaded for filling.
    """
    if dist_from:
        _formula(ws, r, DIST, dist_from, fmt=KM,
                 fill=None if view.distance_km else WARN)
    else:
        _cell(ws, r, 10, view.distance_km, fmt=KM, size=9, border=True,
              align="right", fill=None if view.distance_km else WARN)
    _cell(ws, r, 16, round(view.rask_revenue) if view.rask_revenue else None,
          fmt=MONEY, size=9, border=True, align="right")
    _cell(ws, r, 17, round(view.rask_seats) if view.rask_seats else None,
          fmt="#,##0", size=9, border=True, align="right")
    _formula(ws, r, RASK, _rask_formula([r]), fmt="0.00")


def _pair_rask(ws, r: int, rows: list) -> None:
    """The pair row: its distance, inputs and RASK, all from its directions."""
    if not rows:
        return
    first = rows[0]
    dist = f'=IF(N({DIST}{first})>0,{DIST}{first},'
    dist += (f'IF(N({DIST}{rows[1]})>0,{DIST}{rows[1]},""))'
             if len(rows) > 1 else '"")')
    _formula(ws, r, DIST, dist, fmt=KM, fill=PAPER)
    _formula(ws, r, FARE, "=" + "+".join(f"N({FARE}{x})" for x in rows),
             fmt=MONEY, fill=PAPER)
    _formula(ws, r, SEATS, "=" + "+".join(f"N({SEATS}{x})" for x in rows),
             fmt="#,##0", fill=PAPER)
    _formula(ws, r, RASK, _rask_formula(rows), fmt="0.00", fill=PAPER,
             bold=True)


def _prep(ws) -> None:
    for i, width in enumerate(WIDTHS, start=1):
        ws.column_dimensions[chr(64 + i)].width = width
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "A5"


def _title(ws, text, note) -> None:
    ws.merge_cells(f"A1:{LAST}1")
    _cell(ws, 1, 1, f"  {text}", bold=True, size=16, color="FFFFFF",
          fill=NAVY, align="left")
    ws.row_dimensions[1].height = 34
    ws.merge_cells(f"A2:{LAST}2")
    _cell(ws, 2, 1, f"  {note}", size=9, color=GREY, fill=PAPER,
          align="left", wrap=True)
    ws.row_dimensions[2].height = 30


def _route_row(ws, r: int, view, bold: bool = False, *, label=None,
               fill=None) -> None:
    """One row for a direction or a pair -- both carry the same figures."""
    tr = view.top_rival
    tone = BAD if view.squeezed else None
    _cell(ws, r, 1, label or view.route, bold=True, size=10, border=True,
          fill=fill)
    _cell(ws, r, 2, view.load_factor, fmt=PCT, size=9, border=True,
          align="center",
          fill=BAD if (view.load_factor or 0) >= ro.FULL_ENOUGH else fill)
    _cell(ws, r, 3, round(view.our_seats), size=9, border=True, align="right",
          fill=fill)
    _cell(ws, r, 4, round(view.our_flights, 2), fmt="0.00", size=9,
          border=True, align="center", fill=fill)
    _cell(ws, r, 5, round(view.rival_seats), size=9, border=True,
          align="right", fill=fill)
    _cell(ws, r, 6, round(view.rival_flights, 2), fmt="0.00", size=9,
          border=True, align="center", fill=fill)
    # the number that moves the answer: an ATR against an A320
    _cell(ws, r, 7, view.seat_share, fmt=PCT, size=10, bold=True,
          border=True, align="center", fill=tone or fill)
    _cell(ws, r, 8, view.flight_share, fmt=PCT, size=9, border=True,
          align="center", fill=fill)
    _cell(ws, r, 9, view.aircraft or None, size=9, border=True, fill=fill)
    # distance (col 10), RASK (12) and its inputs (16, 17) are written by
    # _direction_rask / _pair_rask, as live formulas
    _cell(ws, r, 11, round(view.revenue_month) if view.revenue_month else None,
          fmt=MONEY, size=9, border=True, align="right", fill=fill)
    _cell(ws, r, 13, tr.airline if tr else None, size=9, border=True,
          align="center", fill=fill)
    _cell(ws, r, 14, round(tr.seats) if tr else None, size=9, border=True,
          align="right", fill=fill)
    _cell(ws, r, 15, (tr.aircraft or "")[:18] if tr else None, size=9,
          border=True, fill=fill)
    for j in (10, 12, 16, 17, 18, 19, 20):
        _cell(ws, r, j, None, border=True, fill=fill)
    _cell(ws, r, 21, view.verdict(), bold=bold, size=9, fill=tone or fill,
          border=True)


def _pair_rows(ws, r: int, pair, bold: bool = False,
               linked: int | None = None) -> int:
    """The pair's combined row, then outbound, then inbound.

    `linked` is where this pair starts on the Every route sheet. When set,
    the distances are read from there, so a distance is typed in ONE place
    and every sheet's RASK follows it.
    """
    top = r
    _route_row(ws, r, pair, bold=True, label=pair.name, fill=PAPER)
    r += 1
    rows = []
    for k, (arrow, route, view) in enumerate(
            (("→", pair.outbound_route, pair.outbound),
             ("←", pair.inbound_route, pair.inbound)), start=1):
        if view is None:
            _cell(ws, r, 1, f"   {arrow} {route}", size=9, color=GREY,
                  border=True)
            ws.merge_cells(f"B{r}:{LAST}{r}")
            _cell(ws, r, 2, "  not in the market pull", size=9, color=GREY,
                  align="left")
            r += 1
            continue
        _route_row(ws, r, view, bold=bold, label=f"   {arrow} {route}")
        if linked is not None:
            src = f"'{ROUTES_SHEET}'!{DIST}{linked + k}"
            dist_from = f'=IF(N({src})>0,{src},"")'
        elif k == 2 and not view.distance_km and pair.outbound is not None:
            # both ways unknown: the inbound follows the outbound, so one
            # typed distance fills the pair (and can still be overwritten)
            dist_from = f'=IF(N({DIST}{top + 1})>0,{DIST}{top + 1},"")'
        else:
            dist_from = None
        _direction_rask(ws, r, view, dist_from)
        rows.append(r)
        r += 1
    _pair_rask(ws, top, rows)
    return r


def write_advice(ws, r: int, advice: list) -> int:
    """Each suggested change, then the evidence for it, one line each."""
    r = _band(ws, r, "WHAT TO CHANGE, AND WHY",
              "each suggestion with the data behind it — strongest first")
    if not advice:
        _cell(ws, r, 1, "No route calls for a change on this data.",
              size=10, color=GREY)
        return r + 2
    for item in advice:
        tone = BAD if item.kind == "add" else WARN
        _cell(ws, r, 1, item.pair, bold=True, size=11, border=True,
              fill=PAPER)
        ws.merge_cells(f"B{r}:{LAST}{r}")
        _cell(ws, r, 2, f"  {item.action}", bold=True, size=11,
              fill=tone, align="left")
        ws.row_dimensions[r].height = 20
        r += 1
        for reason in item.reasons:
            ws.merge_cells(f"B{r}:{LAST}{r}")
            _cell(ws, r, 2, f"  • {reason}", size=9, align="left",
                  wrap=True)
            ws.row_dimensions[r].height = 30
            r += 1
        r += 1
    return r


def write_summary(ws, res: ro.Result, linked: dict | None = None,
                  advice=()) -> None:
    _prep(ws)
    ws.freeze_panes = "A4"
    lo, hi = (res.window or ("", ""))
    span = (f"{res.load_span[0]} to {res.load_span[1]}" if res.load_span
            else "unknown")
    _title(ws, "ROUTE OPTIMISATION",
           f"Our flying from the load records ({span}, load factor on seats "
           f"{res.basis or 'unknown'}), against every airline selling the "
           f"same leg between {lo} and {hi}.   Seats decide this, not "
           f"departures: an A320 against an ATR is 180 seats to 72, so "
           f"counting flights says we hold a third of a market where we hold "
           f"a tenth of the seats.")

    r = _band(ws, 4, "AT A GLANCE")
    full = [x for x in res.comparable
            if (x.load_factor or 0) >= ro.FULL_ENOUGH]
    r = _kpi_strip(ws, r, [
        ("Routes", len(res.routes), "#,##0", None),
        ("Comparable", len(res.comparable), "#,##0", None),
        ("Full aircraft", len(full), "#,##0", "C00000"),
        ("Full and out-flown", len(res.squeezed), "#,##0", "C00000"),
        ("Too thin to judge", len(res.routes) - len(res.comparable), "#,##0",
         None),
    ])
    r += 1

    if res.warnings:
        r = _band(ws, r, "READ THIS FIRST")
        for w in res.warnings:
            ws.merge_cells(f"A{r}:{LAST}{r}")
            _cell(ws, r, 1, f"  {w}", size=9, fill=WARN, align="left",
                  wrap=True)
            ws.row_dimensions[r].height = 26
            r += 1
        r += 1

    r = write_advice(ws, r, list(advice))

    r = _band(ws, r, "FULL AND OUT-FLOWN",
              "near-full aircraft holding a small share of the seats, in "
              "either direction — each pair with both its directions")
    r = _headers(ws, r, ROUTE_HEADERS)
    if not res.squeezed_pairs:
        _cell(ws, r, 1, "No route is both full and out-flown.", size=10,
              color=GREY)
        r += 1
    for pair in res.squeezed_pairs:
        r = _pair_rows(ws, r, pair, bold=True,
                       linked=(linked or {}).get(pair.outbound_route))


def write_routes(ws, res: ro.Result) -> dict:
    _prep(ws)
    _title(ws, "EVERY ROUTE",
           "Each city pair is shown both ways: first the two directions "
           "combined (shaded), then outbound (→) and inbound (←). Pairs are "
           "ranked by our share of the seats both ways -- counting only a "
           "direction where the search saw rivals, since one showing nobody "
           "is unsold on the site rather than ours alone. A route with too few "
           "observed flights is kept, and said to be too thin to judge, "
           "rather than ranked against the rest.   RASK is a live formula: "
           "a YELLOW distance is missing from the distance table -- type the "
           "km into the outbound (→) row and RASK fills in on every sheet.")
    ws.row_dimensions[2].height = 44
    r = _headers(ws, 4, ROUTE_HEADERS)
    starts = {}
    for pair in res.pairs:
        starts[pair.outbound_route] = r
        r = _pair_rows(ws, r, pair)
    return starts


def write_rivals(ws, res: ro.Result) -> None:
    _prep(ws)
    _title(ws, "WHO FLIES WHAT",
           "One row per airline per leg. Seat counts are published "
           "configurations except where marked 'operator', which is read "
           "from our own filed capacity — the same aircraft type differs by "
           "carrier, so a 737-800 is 162 at Biman and 174 at flydubai.")
    r = _headers(ws, 4, [
        "Route", "Airline", "Flights/day", "Seats/day", "Aircraft",
        "Seat count from", "", "", "", "", "", "", "", "", "", "", "", "",
        "", "", ""])
    views = [v for p in res.pairs for v in p.directions]
    for view in views:
        for rival in view.rivals:
            _cell(ws, r, 1, view.route, size=9, border=True)
            _cell(ws, r, 2, rival.airline, bold=True, size=9, border=True,
                  align="center")
            _cell(ws, r, 3, round(rival.flights, 2), fmt="0.00", size=9,
                  border=True, align="center")
            _cell(ws, r, 4, round(rival.seats), size=9, border=True,
                  align="right")
            _cell(ws, r, 5, (rival.aircraft or "")[:22], size=9, border=True)
            _cell(ws, r, 6, rival.seat_basis, size=9, color=GREY,
                  border=True, align="center",
                  fill=GOOD if rival.seat_basis == "operator" else None)
            for j in range(7, 22):
                _cell(ws, r, j, None, border=True)
            r += 1


def build_workbook(res: ro.Result, out_path, fleet=None,
                   checks=()) -> None:
    from .route_fleet_report import add_fleet_sheet
    from .route_reasons import advise_all

    wb = Workbook()
    wb.active.title = "Summary"
    # routes first: the Summary's distances point at where each pair landed
    starts = write_routes(wb.create_sheet(ROUTES_SHEET), res)
    write_summary(wb["Summary"], res, starts,
                  advise_all(res, checks))
    write_rivals(wb.create_sheet("Who flies what"), res)
    add_fleet_sheet(wb, fleet, list(checks))
    wb.save(str(out_path))


def default_filename() -> str:
    from datetime import date
    return f"Route_Optimisation_{date.today():%b%Y}.xlsx"
