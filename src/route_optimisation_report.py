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

WIDTHS = [12, 8, 10, 11, 11, 10, 9, 10, 9, 10, 13, 13, 11, 10, 10,
          7, 7, 7, 7, 7, 30]

ROUTE_HEADERS = [
    "Route", "Load", "Our seats/day", "Our flights/day", "Rival seats/day",
    "Rival flights", "Seat share", "Flight share", "Aircraft", "Distance km",
    "Base fare/month", "RASK", "Top rival", "Their seats", "Their aircraft",
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
    _cell(ws, r, 10, round(view.distance_km) if view.distance_km else None,
          size=9, border=True, align="right", fill=fill)
    _cell(ws, r, 11, round(view.revenue_month) if view.revenue_month else None,
          fmt=MONEY, size=9, border=True, align="right", fill=fill)
    _cell(ws, r, 12, round(view.rask, 2) if view.rask else None, fmt="0.00",
          size=9, border=True, align="right", fill=fill)
    _cell(ws, r, 13, tr.airline if tr else None, size=9, border=True,
          align="center", fill=fill)
    _cell(ws, r, 14, round(tr.seats) if tr else None, size=9, border=True,
          align="right", fill=fill)
    _cell(ws, r, 15, (tr.aircraft or "")[:18] if tr else None, size=9,
          border=True, fill=fill)
    for j in range(16, 21):
        _cell(ws, r, j, None, border=True, fill=fill)
    _cell(ws, r, 21, view.verdict(), bold=bold, size=9, fill=tone or fill,
          border=True)


def _pair_rows(ws, r: int, pair, bold: bool = False) -> int:
    """The pair's combined row, then outbound, then inbound."""
    _route_row(ws, r, pair, bold=True, label=pair.name, fill=PAPER)
    r += 1
    for arrow, route, view in (("→", pair.outbound_route, pair.outbound),
                               ("←", pair.inbound_route, pair.inbound)):
        if view is None:
            _cell(ws, r, 1, f"   {arrow} {route}", size=9, color=GREY,
                  border=True)
            ws.merge_cells(f"B{r}:{LAST}{r}")
            _cell(ws, r, 2, "  not in the market pull", size=9, color=GREY,
                  align="left")
        else:
            _route_row(ws, r, view, bold=bold, label=f"   {arrow} {route}")
        r += 1
    return r


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
              "near-full aircraft holding a small share of the seats, in "
              "either direction — each pair with both its directions")
    r = _headers(ws, r, ROUTE_HEADERS)
    if not res.squeezed_pairs:
        _cell(ws, r, 1, "No route is both full and out-flown.", size=10,
              color=GREY)
        r += 1
    for pair in res.squeezed_pairs:
        r = _pair_rows(ws, r, pair, bold=True)


def write_routes(ws, res: ro.Result) -> None:
    _prep(ws)
    _title(ws, "EVERY ROUTE",
           "Each city pair is shown both ways: first the two directions "
           "combined (shaded), then outbound (→) and inbound (←). Pairs are "
           "ranked by our share of the seats both ways -- counting only a "
           "direction where the search saw rivals, since one showing nobody "
           "is unsold on the site rather than ours alone. A route with too few "
           "observed flights is kept, and said to be too thin to judge, "
           "rather than ranked against the rest.")
    r = _headers(ws, 4, ROUTE_HEADERS)
    for pair in res.pairs:
        r = _pair_rows(ws, r, pair)


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
