"""The Fleet sheet of the Route Optimisation workbook.

It answers the question the rest of the workbook leaves open: a route is
full and out-flown, but is there an aircraft to fly it more? The answer is
rebuilt from the load workbook's own timetable (see fleet_rotation), so it
is only as current as the last load file given to the app.
"""
from __future__ import annotations

from . import fleet_rotation as fr
from .counter_master import (BAD, GOOD, GREY, LAST, WARN, _band, _cell,
                             _headers)
from .route_optimisation_report import _title

SHEET = "Fleet"
WIDTHS = [18, 26, 11, 13, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10,
          10, 10, 10, 10, 10, 10]

#: A weekday counts as a clean fit when a ground gap fits on this share of
#: the dates observed; below it, the slot is there only some weeks.
MOSTLY = 0.75


def _hm(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}"


def _weekday_cell(ws, r: int, c: int, got) -> None:
    gaps, spares, seen, start, planes = got
    if not seen:
        _cell(ws, r, c, None, border=True)
        return
    many = f" ×{planes}" if planes > 1 else ""
    if gaps and gaps / seen >= MOSTLY:
        text, fill = f"{fr._fmt(start)}{many}  ({gaps}/{seen})", GOOD
    elif gaps:
        text, fill = f"{fr._fmt(start)}{many}  ({gaps}/{seen})", WARN
    elif spares:
        text, fill = f"spare only ({spares}/{seen})", WARN
    else:
        text, fill = "no slot", BAD
    _cell(ws, r, c, text, size=9, border=True, align="center", fill=fill)


def write_fleet(ws, fleet: fr.Fleet, checks: list) -> None:
    for i, width in enumerate(WIDTHS, start=1):
        ws.column_dimensions[chr(64 + i)].width = width
    ws.sheet_view.showGridLines = False
    _title(ws, "FLEET — IS THERE AN AIRCRAFT FOR ONE MORE ROTATION?",
           "Rebuilt from the load workbook's timetable: every leg chained "
           "onto an aircraft of its type in time order. The file names the "
           "type, not the tail, so the fleet is what the busiest day proves "
           "exists. Times are Dhaka time. Maintenance, standby, crew and the "
           "other airport's slots are NOT visible here — a fit means the "
           "aircraft is there, not that planning has agreed to use it.")
    ws.row_dimensions[2].height = 44

    r = _band(ws, 4, "AIRCRAFT SEEN FLYING",
              "per type, over the load window")
    r = _headers(ws, r, ["Type", "Seen on the busiest day", "Typical day",
                         "Chain breaks", "Trust"] + [""] * 16)
    for fam in sorted(fleet.days):
        breaks = fr.chain_breaks(fleet, fam)
        _cell(ws, r, 1, fam, bold=True, size=10, border=True)
        _cell(ws, r, 2, fleet.proven(fam), size=10, border=True,
              align="center")
        _cell(ws, r, 3, fleet.typical(fam), size=10, border=True,
              align="center")
        _cell(ws, r, 4, breaks, size=10, border=True, align="center")
        _cell(ws, r, 5, "clean" if breaks == 0 else "indicative", size=9,
              bold=True, border=True, align="center",
              fill=GOOD if breaks == 0 else WARN)
        r += 1
    r += 1

    r = _band(ws, r, "ONE MORE ROTATION",
              "for each full and out-flown pair — the earliest typical "
              "departure from the base in a ground gap, and on how many of "
              "the weeks seen")
    r = _headers(ws, r, ["Pair", "Aircraft", "Round trip", "Flies on"]
                 + list(fr.WEEKDAYS) + [""] * 10)
    if not checks:
        _cell(ws, r, 1, "No full and out-flown pair to test.", size=10,
              color=GREY)
        r += 1
    for check in checks:
        _cell(ws, r, 1, check.pair, bold=True, size=10, border=True)
        _cell(ws, r, 2, check.fam, size=9, border=True)
        _cell(ws, r, 3, _hm(check.rotation), size=9, border=True,
              align="center")
        _cell(ws, r, 4, " ".join(d[:2] for d in check.flown_days) or "—",
              size=9, border=True, align="center")
        for k, wd in enumerate(fr.WEEKDAYS):
            _weekday_cell(ws, r, 5 + k, check.by_weekday.get(
                wd, (0, 0, 0, None, 0)))
        r += 1
    r += 1

    r = _band(ws, r, "HOW TO READ THIS")
    notes = [
        "How a slot is found: minute by minute, the sheet counts how many "
        "aircraft of the type stand at the base — each landing adds one, "
        "each departure takes one away, whichever aircraft flew it. At the "
        "busiest moment of the day some may still be standing: the "
        "timetable never needed them, so they are treated as the spare "
        "(standby or maintenance) and not used. A rotation fits when, for "
        "its whole length, the count stays above that spare.",
        "GREEN 16:15 (13/13): on 13 of the 13 such weekdays seen, an "
        "aircraft that flies at other hours was free from about 16:15 for "
        "the whole round trip. No extra aircraft is needed. ×2 means two "
        "such rotations would fit at once.",
        "Slots are SHARED between pairs of the same type: DAC ⇄ CCU and "
        "DAC ⇄ CGP showing 15:25 ×2 are offered the same two aircraft — "
        "two extra rotations between them, not two each.",
        "YELLOW: the slot is there only some weeks — the timetable differs "
        "week to week — or the only free aircraft is the spare. RED: no "
        "slot either way.",
        "Why ground time matters: an aircraft back at base cannot leave "
        "again the minute it lands. Without it, a 2-hour gap would look "
        "like room for a 2-hour round trip that cannot actually be flown. "
        "The extra rotation is given the LONGEST turn, 1h15, at every stop "
        "— before it leaves, at the far end, and back at base before the "
        "aircraft's next flight — so a slot shown here fits even on a slow "
        "day. The flights already in the timetable keep their own turns: "
        "they are flown today, so they are taken as proven.",
        "Round trip = block out + 1h15 + block back + 1h15. Block times are "
        "our own from the market pull, corrected for each station's clock. "
        "A slot must leave between 05:00 and 20:30 for the ATR, 23:00 for "
        "the jets.",
        f"The 737-800s and the A320 are one pool: {fr.NARROWBODY} — the load "
        "file labels the return leg by habit, not by what flew, so the two "
        "cannot be told apart on the way home.",
        "'Chain breaks' counts departures no aircraft of the type was "
        "waiting for — a leg missing from the load file or an unrecorded "
        "swap. A type with breaks is indicative only. Refresh by giving the "
        "app the latest load workbook.",
    ]
    if fleet.missing_block:
        notes.append("No block time for: "
                     + ", ".join(sorted(fleet.missing_block))
                     + " — those legs are left out of the chain.")
    for note in notes:
        ws.merge_cells(f"A{r}:{LAST}{r}")
        _cell(ws, r, 1, f"  {note}", size=9, align="left", wrap=True)
        ws.row_dimensions[r].height = 26
        r += 1


def add_fleet_sheet(wb, fleet: fr.Fleet | None, checks: list) -> None:
    if fleet is None or not fleet.days:
        return
    write_fleet(wb.create_sheet(SHEET), fleet, checks)
