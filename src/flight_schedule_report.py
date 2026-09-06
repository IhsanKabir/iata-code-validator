"""The Flight Schedule History workbook.

Sheets:
  Summary        the period, what was read, and what is NOT counted
  By route       most unstable route first
  By flight      one row per flight per operating date
  Every change   one row per distinct change, with old -> new and who made it
  Schedule       the roster: flight, legs, scheduled times, aircraft
  Unrecognised   event types no grammar matched, so the blind spot is visible

Every caveat the aggregation carries is printed on the Summary rather than
left in a docstring: whether the passenger counts rest on observed repetition,
whether a roster was loaded, and that a change made before any booking existed
leaves no row to count.
"""
from __future__ import annotations

from datetime import date

from openpyxl import Workbook

from .counter_master import (BAD, COLS, GOOD, GREY, LAST, NAVY, PAPER, PCT,
                             WARN, _band, _cell, _headers, _kpi_strip)
from .flight_change_auth import KIND_CANCEL, KIND_TIME, KIND_TRANSFER

MINUTES = "#,##0"

KIND_LABEL = {
    KIND_TIME: "Time changed",
    KIND_TRANSFER: "Swapped / replaced",
    KIND_CANCEL: "Cancelled",
}


def _mins(v):
    return round(v) if v is not None else None


def _fmt_dt(v):
    return v.strftime("%d %b %H:%M") if v is not None else ""


def _prep(ws):
    for col, width in COLS:
        ws.column_dimensions[col].width = width
    ws.sheet_view.showGridLines = False


def _title(ws, text, note):
    ws.merge_cells(f"A1:{LAST}1")
    _cell(ws, 1, 1, f"  {text}", bold=True, size=16, color="FFFFFF", fill=NAVY,
          align="left")
    ws.row_dimensions[1].height = 34
    ws.merge_cells(f"A2:{LAST}2")
    _cell(ws, 2, 1, f"  {note}", size=9, color=GREY, fill=PAPER, align="left")
    ws.row_dimensions[2].height = 17


def write_summary(ws, res, *, month: int, year: int, roster=None) -> None:
    _prep(ws)
    period = f"{date(year, month, 1):%B %Y}"
    _title(ws, f"FLIGHT SCHEDULE HISTORY  ·  {period}",
           f"{res.events_read:,} history row(s) read, "
           f"{res.events_with_change:,} carried a recognised schedule change, "
           f"collapsed into {len(res.changes):,} distinct change(s) across "
           f"{len(res.flights):,} flight(s) and {len(res.routes):,} route(s).")

    r = 4
    r = _band(ws, r, "AT A GLANCE")
    r = _kpi_strip(ws, r, [
        ("Distinct changes", len(res.changes), "#,##0", "C00000"),
        ("Time changed", res.n_time, "#,##0", "BF8F00"),
        ("Swapped / replaced", res.n_swaps, "#,##0", "C00000"),
        ("Cancelled", res.n_cancelled, "#,##0", "C00000"),
        ("Flights affected", len(res.flights), "#,##0", None),
        ("Routes affected", len(res.routes), "#,##0", None),
        ("PNRs seen affected", res.passengers, "#,##0", None),
    ])
    r += 1

    r = _band(ws, r, "HOW TO READ THESE NUMBERS")
    notes = [
        ("Magnitudes cover time changes only",
         "A swap's 'shift' is the gap between two DATES — one real move was "
         "47,520 minutes. Averaging that with a 65-minute retime would report "
         "a three-thousand-minute delay, so mean, median and worst shift are "
         "computed over time changes alone. Swaps and cancellations are "
         "counted, never averaged."),
        ("Passengers affected is a floor" if not res.repetition_observed
         else "Passengers affected is counted from repeated rows",
         "One schedule change is written once per affected PNR, so the number "
         "of rows collapsed is the number of passengers. No repetition was "
         "seen in these files, so each count is a floor rather than a census."
         if not res.repetition_observed else
         "Repetition was observed in these files, so the counts come from the "
         "rows actually collapsed."),
        ("Share of flights changed needs the roster" if not res.roster_loaded
         else "Share of flights changed uses the loaded roster",
         "Without the flight roster there is no denominator, so the share is "
         "left blank rather than divided by the flights we happened to see "
         "change — which would always read as 100%."
         if not res.roster_loaded else
         "The roster supplies how many flights were scheduled on each route."),
        ("A change before any booking leaves no row",
         "This log is attached to PNRs. A schedule move made before the "
         "flight had any bookings is invisible here, so these counts are a "
         "lower bound on what operations actually did."),
    ]
    for head, body in notes:
        _cell(ws, r, 1, head, bold=True, size=9, border=True, color="C00000")
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=21)
        _cell(ws, r, 2, body, size=8, color=GREY, border=True, wrap=True)
        ws.row_dimensions[r].height = 26
        r += 1
    ws.freeze_panes = "A4"


def write_by_route(ws, res) -> None:
    _prep(ws)
    _title(ws, "BY ROUTE  ·  most unstable first",
           "Mean, median and worst shift are minutes, over TIME changes only. "
           "Swaps and cancellations are counts.")
    r = 4
    r = _headers(ws, r, [
        "Route", "Flights changed", "Flights scheduled", "Share changed",
        "Changes", "Time changed", "Swapped", "Cancelled",
        "Mean shift (min)", "Median shift (min)", "Worst shift (min)",
        "Moved later", "Moved earlier", "PNRs affected"] + [""] * 7)
    for rec in sorted(res.routes.values(),
                      key=lambda x: (-x.n_changes, x.route)):
        _cell(ws, r, 1, rec.route, bold=True, size=10, border=True)
        _cell(ws, r, 2, rec.flights_changed, size=9, border=True,
              align="center")
        _cell(ws, r, 3, rec.flights_scheduled, size=9, border=True,
              align="center")
        _cell(ws, r, 4, rec.share_changed, fmt=PCT, size=9, border=True,
              align="center")
        _cell(ws, r, 5, rec.n_changes, bold=True, size=10, border=True,
              align="center", color="C00000")
        _cell(ws, r, 6, rec.n_time or None, size=9, border=True,
              align="center")
        _cell(ws, r, 7, rec.n_swaps or None, size=9, border=True,
              align="center", color="C00000" if rec.n_swaps else None)
        _cell(ws, r, 8, rec.n_cancelled or None, size=9, border=True,
              align="center", color="C00000" if rec.n_cancelled else None)
        _cell(ws, r, 9, _mins(rec.mean_shift), fmt=MINUTES, size=9,
              border=True, align="center")
        _cell(ws, r, 10, _mins(rec.median_shift), fmt=MINUTES, size=9,
              border=True, align="center")
        _cell(ws, r, 11, _mins(rec.worst_shift), fmt=MINUTES, size=9,
              border=True, align="center")
        _cell(ws, r, 12, rec.moved_later or None, size=9, border=True,
              align="center")
        _cell(ws, r, 13, rec.moved_earlier or None, size=9, border=True,
              align="center")
        _cell(ws, r, 14, rec.passengers or None, size=9, border=True,
              align="center")
        for j in range(15, 22):
            _cell(ws, r, j, None, border=True)
        r += 1
    ws.freeze_panes = "A5"


def write_by_flight(ws, res) -> None:
    _prep(ws)
    _title(ws, "BY FLIGHT  ·  one row per flight per operating date",
           "A flight whose number the log did not write is identified by its "
           "route and the date it was due to depart.")
    r = 4
    r = _headers(ws, r, [
        "Flight", "Date", "Route", "Changes", "Time changed", "Swapped",
        "Cancelled", "Worst shift (min)", "PNRs affected"] + [""] * 12)
    for rec in sorted(res.flights.values(),
                      key=lambda x: (-x.n_changes, x.flight_number,
                                     x.flight_date)):
        _cell(ws, r, 1, rec.flight_number, bold=True, size=10, border=True)
        _cell(ws, r, 2, rec.flight_date, size=9, border=True, align="center")
        _cell(ws, r, 3, rec.route, size=9, border=True)
        _cell(ws, r, 4, rec.n_changes, bold=True, size=10, border=True,
              align="center", color="C00000")
        _cell(ws, r, 5, rec.n_time or None, size=9, border=True,
              align="center")
        _cell(ws, r, 6, rec.n_swaps or None, size=9, border=True,
              align="center")
        _cell(ws, r, 7, rec.n_cancelled or None, size=9, border=True,
              align="center")
        _cell(ws, r, 8, _mins(rec.worst_shift), fmt=MINUTES, size=9,
              border=True, align="center")
        _cell(ws, r, 9, rec.passengers or None, size=9, border=True,
              align="center")
        for j in range(10, 22):
            _cell(ws, r, j, None, border=True)
        r += 1
    ws.freeze_panes = "A5"


def write_changes(ws, res, *, limit: int = 2000) -> None:
    _prep(ws)
    _title(ws, "EVERY CHANGE  ·  newest last",
           "One row per distinct change, however many PNRs recorded it. "
           "'Rows collapsed' is how many history rows carried this same "
           "change.")
    r = 4
    hdr = r
    r = _headers(ws, r, [
        "When changed", "Flight", "Date", "Route", "What changed",
        "From", "To", "Shift (min)", "Direction", "Notice given",
        "PNRs", "Rows collapsed", "Changed by", "Detail"] + [""] * 7)
    for c in res.changes[:limit]:
        tone = (BAD if c.kind in (KIND_TRANSFER, KIND_CANCEL)
                else WARN if c.kind == KIND_TIME else None)
        _cell(ws, r, 1, _fmt_dt(c.when), size=9, border=True, align="center")
        _cell(ws, r, 2, c.flight_number, bold=True, size=10, border=True)
        _cell(ws, r, 3, c.flight_date, size=9, border=True, align="center")
        _cell(ws, r, 4, c.route, size=9, border=True)
        _cell(ws, r, 5, KIND_LABEL.get(c.kind, c.kind), bold=True, size=9,
              border=True, align="center", color="FFFFFF", fill=tone)
        _cell(ws, r, 6, _fmt_dt(c.original_dep), size=9, border=True,
              align="center")
        _cell(ws, r, 7, _fmt_dt(c.revised_dep), size=9, border=True,
              align="center")
        # a swap's gap is a date distance, not a retime -- left blank here so
        # it can never be read as a delay
        _cell(ws, r, 8, _mins(c.shift_minutes) if c.is_measurable else None,
              fmt=MINUTES, size=9, border=True, align="center")
        _cell(ws, r, 9, c.direction, size=9, border=True, align="center")
        _cell(ws, r, 10, c.lead, size=8, border=True, align="center")
        _cell(ws, r, 11, c.passengers or None, size=9, border=True,
              align="center")
        _cell(ws, r, 12, c.rows, size=9, border=True, align="center")
        _cell(ws, r, 13, c.changed_by or None, size=8, border=True)
        _cell(ws, r, 14, (c.detail or "")[:80] or None, size=8, color=GREY,
              border=True)
        for j in range(15, 22):
            _cell(ws, r, j, None, border=True)
        r += 1
    if res.changes:
        ws.auto_filter.ref = f"A{hdr}:N{r - 1}"
    ws.freeze_panes = f"A{hdr + 1}"


def write_schedule(ws, roster) -> None:
    _prep(ws)
    roster = list(roster or [])
    _title(ws, "SCHEDULE  ·  the flights as the listing states them",
           f"{len(roster):,} leg(s). A blank time means the listing did not "
           f"carry one, never that the flight has no time.")
    r = 4
    hdr = r
    r = _headers(ws, r, ["Flight", "Date", "From", "To", "Scheduled departure",
                         "Scheduled arrival", "Aircraft"] + [""] * 14)
    for f in roster:
        _cell(ws, r, 1, f.flight_number, bold=True, size=10, border=True)
        _cell(ws, r, 2, f.flight_date, size=9, border=True, align="center")
        _cell(ws, r, 3, f.origin, size=9, border=True, align="center")
        _cell(ws, r, 4, f.destination, size=9, border=True, align="center")
        _cell(ws, r, 5, getattr(f, "sched_dep", "") or None, size=9,
              border=True, align="center")
        _cell(ws, r, 6, getattr(f, "sched_arr", "") or None, size=9,
              border=True, align="center")
        _cell(ws, r, 7, getattr(f, "aircraft", "") or None, size=9,
              border=True)
        for j in range(8, 22):
            _cell(ws, r, j, None, border=True)
        r += 1
    if roster:
        ws.auto_filter.ref = f"A{hdr}:G{r - 1}"
    ws.freeze_panes = f"A{hdr + 1}"


def write_unrecognised(ws, res) -> None:
    _prep(ws)
    total = sum(res.unrecognised.values())
    _title(ws, "UNRECOGNISED  ·  the blind spot, stated",
           f"{total:,} row(s) carried an event type no grammar matched. They "
           f"are not schedule changes as far as this report can tell — which "
           f"is different from knowing they are not.")
    r = 4
    r = _headers(ws, r, ["Event type", "Rows"] + [""] * 19)
    for etype, n in sorted(res.unrecognised.items(), key=lambda kv: -kv[1]):
        _cell(ws, r, 1, etype, size=9, border=True)
        _cell(ws, r, 2, n, size=9, border=True, align="center")
        for j in range(3, 22):
            _cell(ws, r, j, None, border=True)
        r += 1
    ws.freeze_panes = "A5"


def build_workbook(res, out_path, *, month: int, year: int, roster=None):
    """Write every sheet. Returns the path written."""
    wb = Workbook()
    write_summary(wb.active, res, month=month, year=year, roster=roster)
    wb.active.title = "Summary"
    write_by_route(wb.create_sheet("By route"), res)
    write_by_flight(wb.create_sheet("By flight"), res)
    write_changes(wb.create_sheet("Every change"), res)
    if roster:
        write_schedule(wb.create_sheet("Schedule"), roster)
    if res.unrecognised:
        write_unrecognised(wb.create_sheet("Unrecognised"), res)
    wb.save(out_path)
    return out_path


def build_master_path(out_dir, month: int, year: int):
    from pathlib import Path
    return Path(out_dir) / f"Flight_Schedule_History_{date(year, month, 1):%b%Y}.xlsx"
