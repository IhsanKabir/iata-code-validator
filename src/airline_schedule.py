"""The airline schedule, drawn from a Flight Loads pull.

A Flight Loads search already returns everything a schedule needs -- the
listing hands back the flight, the day, each leg, the aircraft, the tail
number and the leg's own local time range. So this asks for nothing from
Zenith: it reshapes rows that are already in memory after a search.

Four things measured on a real 2,528-row export shape this module, because
each one produces a wrong schedule if ignored:

* `departure_time` is PER FLIGHT, not per leg. BS344 on 01/09 reports 12:30
  against both its legs, while the legs actually ran 12:30-19:50 (DXB-CGP)
  and 20:40-21:35 (CGP-DAC). Every leg time here comes from the leg's own
  `leg_local_time_range`.
* 235 of those legs land before they depart by the clock -- they cross
  midnight. An arrival is marked as next-day rather than yielding a negative
  duration.
* 13 legs carry a date LATER than the flight date (BS350 filed under 03/09
  actually operates 04/09). The leg's own date wins, and the difference is
  reported rather than smoothed away.
* Registration is blank on 27 rows and inventory status on 74. Blank is
  written as "not written", never as a fact.

The one thing deliberately NOT computed as fact is block time. Departure and
arrival are local clocks at two different airports, so subtracting them is
only a real duration when both ends share a timezone. The column is named for
what it is and carries that caveat.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

NOT_WRITTEN = "not written"

#: "01/09/2026 05:00 - 07:00" -- validated against 2,528 real rows, none of
#: which deviated from this shape.
_RANGE_RE = re.compile(
    r"^\s*(?P<date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<dep>\d{1,2}:\d{2})\s*-\s*(?P<arr>\d{1,2}:\d{2})\s*$")

_BD_AIRPORTS = frozenset({"DAC", "CGP", "CXB", "ZYL", "JSR", "RJH", "BZL",
                          "SPD"})

HEADERS = [
    "Flight", "Day", "Leg date", "Route", "From", "To",
    "Scheduled departure", "Scheduled arrival", "Arrives", "Sector",
    "Aircraft", "Registration", "Seats", "Inventory status",
    "Leg order", "Elapsed on local clocks", "Filed under",
]

NAVY = "1F3864"
PAPER = "F2F5FA"
GREY = "808080"
WARN = "BF8F00"


def _txt(v) -> str:
    return "" if v is None else str(v).strip()


def parse_leg_range(value):
    """('01/09/2026 05:00 - 07:00') -> (date, dep_time, arr_time, next_day).

    Returns (None, '', '', False) when the cell cannot be read -- never a
    guessed time.
    """
    m = _RANGE_RE.match(_txt(value))
    if not m:
        return None, "", "", False
    try:
        d = datetime.strptime(m.group("date"), "%d/%m/%Y").date()
    except ValueError:
        return None, "", "", False
    dep, arr = m.group("dep"), m.group("arr")
    # A leg that lands earlier on the clock than it left crossed midnight.
    next_day = _minutes(arr) < _minutes(dep)
    return d, dep, arr, next_day


def _minutes(hhmm: str) -> int:
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return 0


def _elapsed(dep: str, arr: str, next_day: bool) -> str:
    """Clock-to-clock gap, formatted h:mm. NOT block time -- the two ends can
    sit in different timezones, which is why the column says so."""
    if not dep or not arr:
        return ""
    mins = _minutes(arr) - _minutes(dep) + (24 * 60 if next_day else 0)
    if mins <= 0:
        return ""
    return f"{mins // 60}:{mins % 60:02d}"


def _sector(origin: str, dest: str) -> str:
    o, d = origin.upper(), dest.upper()
    if not o or not d:
        return NOT_WRITTEN
    return "Domestic" if o in _BD_AIRPORTS and d in _BD_AIRPORTS else "International"


def _capacity(seats_available: str):
    """'13/410 97%' -> 410. None when unreadable, never zero."""
    m = re.match(r"\s*-?\d+\s*/\s*(\d+)", _txt(seats_available))
    return int(m.group(1)) if m else None


@dataclass
class ScheduledLeg:
    """One leg of one flight on one operating date, cabins collapsed."""

    flight_number: str
    day_of_week: str
    leg_date: date | None
    flight_date: str            # as the listing filed it
    route: str
    origin: str
    destination: str
    dep: str
    arr: str
    next_day: bool
    aircraft: str
    registration: str
    inventory_status: str
    seats: int | None = None
    cabins: int = 0
    order: int = 0              # 1-based position within the flight that day
    #: Cabins already counted into `seats`. No duplicate cabin row appeared in
    #: the 2,528-row export this was built against, but summing one twice would
    #: report an aircraft bigger than it is.
    counted_cabins: set = field(default_factory=set)

    @property
    def sector(self) -> str:
        return _sector(self.origin, self.destination)

    @property
    def elapsed(self) -> str:
        return _elapsed(self.dep, self.arr, self.next_day)

    @property
    def date_differs(self) -> bool:
        """The leg operates on a different date than the flight is filed under.

        13 real legs do this; it is a fact about the schedule, not an error to
        paper over.
        """
        if self.leg_date is None or not self.flight_date:
            return False
        return self.leg_date.strftime("%d/%m/%Y") != self.flight_date


@dataclass
class ScheduleResult:
    legs: list = field(default_factory=list)
    rows_read: int = 0
    unreadable_ranges: int = 0
    date_from: str = ""
    date_to: str = ""

    @property
    def flights(self) -> int:
        return len({(l.flight_number, l.flight_date) for l in self.legs})

    @property
    def _dates(self) -> list:
        return sorted({l.leg_date for l in self.legs if l.leg_date})

    @property
    def first_date(self):
        got = self._dates
        return got[0] if got else None

    @property
    def last_date(self):
        got = self._dates
        return got[-1] if got else None

    @property
    def dates_covered(self) -> int:
        return len(self._dates)

    @property
    def covered_span(self) -> str:
        """The period these legs cover, read off the legs themselves.

        The heading states this rather than a typed range: the same rows were
        once labelled 01/09 to 06/09 while carrying all 30 dates of September,
        and a heading naming a period it does not contain is worse than none.
        """
        if self.first_date is None:
            return "no dated legs"
        if self.first_date == self.last_date:
            return f"{self.first_date:%d %b %Y}"
        return f"{self.first_date:%d %b %Y} to {self.last_date:%d %b %Y}"

    @property
    def searched_span(self) -> str:
        if self.date_from and self.date_to:
            return f"{self.date_from} to {self.date_to}"
        return ""

    @property
    def range_disagrees(self) -> bool:
        """Was a period asked for that these legs do not sit inside?"""
        if self.first_date is None or not (self.date_from and self.date_to):
            return False
        try:
            want_a = datetime.strptime(self.date_from, "%d/%m/%Y").date()
            want_b = datetime.strptime(self.date_to, "%d/%m/%Y").date()
        except ValueError:
            return False
        return self.first_date < want_a or self.last_date > want_b

    @property
    def routes(self) -> Counter:
        return Counter(l.route for l in self.legs)

    @property
    def multi_leg(self) -> int:
        seen: Counter = Counter()
        for l in self.legs:
            seen[(l.flight_number, l.flight_date)] += 1
        return sum(1 for n in seen.values() if n > 1)

    @property
    def crossing_midnight(self) -> int:
        return sum(1 for l in self.legs if l.next_day)

    @property
    def filed_under_another_date(self) -> int:
        return sum(1 for l in self.legs if l.date_differs)


def build(rows, *, date_from: str = "", date_to: str = "") -> ScheduleResult:
    """Collapse Flight Loads rows into one leg per (flight, date, route).

    Rows arrive one per (flight, leg, cabin); a schedule wants the leg, so
    cabins are merged and their seats summed.
    """
    res = ScheduleResult(date_from=date_from, date_to=date_to)
    merged: dict = {}
    for r in rows:
        res.rows_read += 1
        flight = "".join(_txt(getattr(r, "flight_number", "")).upper().split())
        fdate = _txt(getattr(r, "flight_date", ""))
        route = _txt(getattr(r, "leg_route", "")).upper()
        leg_date, dep, arr, next_day = parse_leg_range(
            getattr(r, "leg_local_time_range", ""))
        if leg_date is None:
            res.unreadable_ranges += 1
        key = (flight, fdate, route)
        leg = merged.get(key)
        if leg is None:
            leg = merged[key] = ScheduledLeg(
                flight_number=flight or NOT_WRITTEN,
                day_of_week=_txt(getattr(r, "day_of_week", "")),
                leg_date=leg_date, flight_date=fdate,
                route=route or NOT_WRITTEN,
                origin=_txt(getattr(r, "leg_origin", "")).upper(),
                destination=_txt(getattr(r, "leg_destination", "")).upper(),
                dep=dep, arr=arr, next_day=next_day,
                aircraft=_txt(getattr(r, "aircraft", "")) or NOT_WRITTEN,
                registration=_txt(getattr(r, "registration", "")) or NOT_WRITTEN,
                inventory_status=(_txt(getattr(r, "inventory_status", ""))
                                  or NOT_WRITTEN),
            )
        cabin = _txt(getattr(r, "leg_cabin", "")) or "(cabin not written)"
        if cabin in leg.counted_cabins:
            continue                    # the same cabin twice: already counted
        leg.counted_cabins.add(cabin)
        leg.cabins += 1
        cap = _capacity(getattr(r, "seats_available", ""))
        if cap is not None:
            leg.seats = (leg.seats or 0) + cap

    # Order the legs of each flight by when the leg itself departs -- the
    # per-flight departure_time is identical across legs and cannot order them.
    by_flight: dict = {}
    for leg in merged.values():
        by_flight.setdefault((leg.flight_number, leg.flight_date), []).append(leg)
    for group in by_flight.values():
        group.sort(key=lambda l: (l.leg_date is None, l.leg_date or date.min,
                                  not l.dep, _minutes(l.dep)))
        for i, leg in enumerate(group, start=1):
            leg.order = i

    # Undated legs sort LAST. Treating an unreadable time as 00:00 put them at
    # the top of the sheet, where they read as the day's first departures.
    res.legs = sorted(
        merged.values(),
        key=lambda l: (l.leg_date is None, l.leg_date or date.min,
                       not l.dep, _minutes(l.dep), l.flight_number, l.order))
    return res


# ---------------------------------------------------------------------------
# the workbook
# ---------------------------------------------------------------------------


def _head(ws, r, labels):
    for j, label in enumerate(labels, start=1):
        c = ws.cell(row=r, column=j, value=label)
        c.font = Font(bold=True, size=9, color="FFFFFF", name="Segoe UI")
        c.fill = PatternFill("solid", fgColor=NAVY)
        c.alignment = Alignment(horizontal="center", wrap_text=True)
    return r + 1


def _note(ws, r, text, span=17):
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=span)
    c = ws.cell(row=r, column=1, value=f"  {text}")
    c.font = Font(size=9, color=GREY, name="Segoe UI")
    c.fill = PatternFill("solid", fgColor=PAPER)
    c.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[r].height = 26
    return r + 1


def write_airline_schedule(path, rows, *, date_from: str = "",
                           date_to: str = "") -> ScheduleResult:
    """Write the schedule workbook.

    Returns what it wrote, so a caller reporting on the export does not have to
    parse every row a second time.
    """
    res = build(rows, date_from=date_from, date_to=date_to)

    wb = Workbook()
    ws = wb.active
    ws.title = "Airline Schedule"
    ws.sheet_view.showGridLines = False

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=17)
    t = ws.cell(row=1, column=1,
                value=f"  AIRLINE SCHEDULE  ·  {res.covered_span}"
                      + ("   ·   CHECK THE RANGE" if res.range_disagrees
                         else ""))
    t.font = Font(bold=True, size=15, color="FFFFFF", name="Segoe UI")
    t.fill = PatternFill("solid", fgColor=NAVY)
    ws.row_dimensions[1].height = 30

    r = _note(
        ws, 2,
        f"{len(res.legs):,} leg(s) across {res.flights:,} flight(s), drawn from "
        f"the {res.rows_read:,} row(s) this Flight Loads search already "
        f"returned — nothing was fetched again. The heading is the period these "
        f"legs COVER ({res.dates_covered:,} date(s)), read off the legs "
        f"themselves rather than a range typed into a box."
        + (f"  You searched {res.searched_span}, which these legs do not sit "
           f"inside — they are the rows the last pull returned, so the search "
           f"boxes may have changed since."
           if res.range_disagrees
           else f"  Searched {res.searched_span}." if res.searched_span else "")
        + f"  Times are each LEG's own local "
        f"range, not the flight's single departure time, which repeats across "
        f"legs and cannot order them."
        + (f"  {res.crossing_midnight:,} leg(s) arrive the next day."
           if res.crossing_midnight else "")
        + (f"  {res.filed_under_another_date:,} leg(s) operate on a date later "
           f"than the one they are filed under — see 'Filed under'."
           if res.filed_under_another_date else "")
        + (f"  {res.unreadable_ranges:,} row(s) carried no readable time range."
           if res.unreadable_ranges else ""))
    r = _note(
        ws, r,
        "'Elapsed on local clocks' is exactly that: arrival clock minus "
        "departure clock. The two ends can sit in different timezones, so it "
        "is NOT block time and must not be read as one.")
    r += 1

    r = _head(ws, r, HEADERS)
    first = r
    for leg in res.legs:
        vals = [
            leg.flight_number, leg.day_of_week,
            leg.leg_date.strftime("%d/%m/%Y") if leg.leg_date else NOT_WRITTEN,
            leg.route, leg.origin, leg.destination, leg.dep, leg.arr,
            "next day" if leg.next_day else "same day",
            leg.sector, leg.aircraft, leg.registration, leg.seats,
            leg.inventory_status, leg.order, leg.elapsed,
            leg.flight_date if leg.date_differs else "",
        ]
        for j, v in enumerate(vals, start=1):
            c = ws.cell(row=r, column=j, value=v if v != "" else None)
            c.font = Font(size=9, name="Segoe UI",
                          color=(WARN if j in (9, 17) and v not in ("", "same day")
                                 else GREY if v == NOT_WRITTEN else "000000"))
            if j in (2, 3, 5, 6, 7, 8, 9, 13, 15, 16):
                c.alignment = Alignment(horizontal="center")
        r += 1
    if res.legs:
        ws.auto_filter.ref = f"A{first - 1}:Q{r - 1}"
    ws.freeze_panes = f"A{first}"
    for col, w in {1: 10, 2: 7, 3: 12, 4: 11, 7: 12, 8: 12, 9: 10, 10: 13,
                   11: 18, 12: 13, 13: 8, 14: 16, 15: 9, 16: 12,
                   17: 12}.items():
        ws.column_dimensions[get_column_letter(col)].width = w

    _write_by_route(wb.create_sheet("By route"), res)
    wb.save(path)
    return res


def _write_by_route(ws, res: ScheduleResult) -> None:
    """How often the airline flies each route across the period searched."""
    ws.sheet_view.showGridLines = False
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=8)
    t = ws.cell(row=1, column=1, value="  BY ROUTE  ·  most frequent first")
    t.font = Font(bold=True, size=15, color="FFFFFF", name="Segoe UI")
    t.fill = PatternFill("solid", fgColor=NAVY)
    ws.row_dimensions[1].height = 30
    r = _note(ws, 2,
              "One row per route. 'Departures' counts legs in the period "
              "searched, so a route flown twice a day shows twice the days.",
              span=8)
    r += 1
    r = _head(ws, r, ["Route", "Sector", "Departures", "Days flown",
                      "First", "Last", "Aircraft seen", "Earliest / latest"])
    groups: dict = {}
    for leg in res.legs:
        groups.setdefault(leg.route, []).append(leg)
    for route, legs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        dates = sorted({l.leg_date for l in legs if l.leg_date})
        deps = sorted({l.dep for l in legs if l.dep})
        types = sorted({l.aircraft for l in legs
                        if l.aircraft and l.aircraft != NOT_WRITTEN})
        vals = [
            route, legs[0].sector, len(legs), len(dates),
            dates[0].strftime("%d/%m/%Y") if dates else "",
            dates[-1].strftime("%d/%m/%Y") if dates else "",
            ", ".join(types)[:60] or NOT_WRITTEN,
            f"{deps[0]} / {deps[-1]}" if deps else "",
        ]
        for j, v in enumerate(vals, start=1):
            c = ws.cell(row=r, column=j, value=v if v != "" else None)
            c.font = Font(size=9, name="Segoe UI",
                          bold=(j == 1),
                          color=GREY if v == NOT_WRITTEN else "000000")
            if j in (2, 3, 4, 5, 6, 8):
                c.alignment = Alignment(horizontal="center")
        r += 1
    for col, w in {1: 12, 2: 13, 3: 11, 4: 11, 5: 12, 6: 12, 7: 40,
                   8: 16}.items():
        ws.column_dimensions[get_column_letter(col)].width = w
    ws.freeze_panes = "A5"
