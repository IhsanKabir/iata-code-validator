"""The Route Optimisation workbook.

Sheets:
  Summary        what was compared, and the routes worth acting on
  Every route    one row per directed route, ranked by seat share
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

WIDTHS = [12, 8, 10, 11, 11, 10, 9, 10, 9, 10, 13, 13, 11, 10, 10,
          7, 7, 7, 7, 7, 30]

ROUTE_HEADERS = [
    "Route", "Load", "Our seats/day", "Our flights/day", "Rival seats/day",
    "Rival flights", "Seat share", "Flight share", "Aircraft", "Distance km",
    "Revenue/month", "RASK", "Top rival", "Their seats", "Their aircraft",
    "", "", "", "", "", "Verdict"]


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


def _route_row(ws, r: int, view, bold: bool = False) -> None:
    tr = view.top_rival
    tone = BAD if view.squeezed else None
    _cell(ws, r, 1, view.route, bold=True, size=10, border=True)
    _cell(ws, r, 2, view.load_factor, fmt=PCT, size=9, border=True,
          align="center",
          fill=BAD if (view.load_factor or 0) >= ro.FULL_ENOUGH else None)
    _cell(ws, r, 3, round(view.our_seats), size=9, border=True, align="right")
    _cell(ws, r, 4, round(view.our_flights, 2), fmt="0.00", size=9,
          border=True, align="center")
    _cell(ws, r, 5, round(view.rival_seats), size=9, border=True,
          align="right")
    _cell(ws, r, 6, round(view.rival_flights, 2), fmt="0.00", size=9,
          border=True, align="center")
    # the number that moves the answer: an ATR against an A320
    _cell(ws, r, 7, view.seat_share, fmt=PCT, size=10, bold=True,
          border=True, align="center", fill=tone)
    _cell(ws, r, 8, view.flight_share, fmt=PCT, size=9, border=True,
          align="center")
    _cell(ws, r, 9, view.aircraft or None, size=9, border=True)
    _cell(ws, r, 10, round(view.distance_km) if view.distance_km else None,
          size=9, border=True, align="right")
    _cell(ws, r, 11, round(view.revenue_month) if view.revenue_month else None,
          fmt=MONEY, size=9, border=True, align="right")
    _cell(ws, r, 12, round(view.rask, 2) if view.rask else None, fmt="0.00",
          size=9, border=True, align="right")
    _cell(ws, r, 13, tr.airline if tr else None, size=9, border=True,
          align="center")
    _cell(ws, r, 14, round(tr.seats) if tr else None, size=9, border=True,
          align="right")
    _cell(ws, r, 15, (tr.aircraft or "")[:18] if tr else None, size=9,
          border=True)
    for j in range(16, 21):
        _cell(ws, r, j, None, border=True)
    _cell(ws, r, 21, view.verdict(), bold=bold, size=9, fill=tone,
          border=True)


def write_summary(ws, res: ro.Result) -> None:
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

    r = _band(ws, r, "FULL AND OUT-FLOWN",
              "near-full aircraft holding a small share of the seats — "
              "smallest share first")
    r = _headers(ws, r, ROUTE_HEADERS)
    if not res.squeezed:
        _cell(ws, r, 1, "No route is both full and out-flown.", size=10,
              color=GREY)
        r += 1
    for view in res.squeezed:
        _route_row(ws, r, view, bold=True)
        r += 1


def write_routes(ws, res: ro.Result) -> None:
    _prep(ws)
    _title(ws, "EVERY ROUTE",
           "Ranked by our share of the seats. A route with too few observed "
           "flights is kept, and said to be too thin to judge, rather than "
           "ranked against the rest.")
    r = _headers(ws, 4, ROUTE_HEADERS)
    for view in res.routes:
        _route_row(ws, r, view)
        r += 1


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
    for view in res.routes:
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


def build_workbook(res: ro.Result, out_path) -> None:
    wb = Workbook()
    write_summary(wb.active, res)
    wb.active.title = "Summary"
    write_routes(wb.create_sheet("Every route"), res)
    write_rivals(wb.create_sheet("Who flies what"), res)
    wb.save(str(out_path))


def default_filename() -> str:
    from datetime import date
    return f"Route_Optimisation_{date.today():%b%Y}.xlsx"
