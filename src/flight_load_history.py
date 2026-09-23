"""Daily flight loads, 2024 to now, out of the analysis workbook.

The workbook is laid out for reading, not for computing: one sheet per year,
one ROW per flight number, and one six-column BLOCK per date running sideways
-- 2,794 columns across 230 dates on the 2026 sheet alone. This turns that
into one row per flight per date, which every later question needs.

Three things it refuses to do, each because the sheet invites the mistake:

* A blank block is not a load of zero. A flight that did not operate that day
  has no capacity, no time and no passengers, and averaging a zero into its
  load factor would drag the route down for days it never flew. Those blocks
  are skipped and counted.
* The stored Load Factor column is not trusted. It carries '#DIV/0!' wherever
  capacity was blank, and a string in a numeric column is how a spreadsheet
  error becomes a reported figure. Load factor is recomputed from flown over
  capacity, and is None where capacity is absent.
* A date header is read as day-first. '03/02/2026 (Tuesday)' is the third of
  February -- and it says Tuesday, which the third is and the second is not,
  so the weekday in the label is used to CHECK the reading rather than
  decorate it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

#: Flight number and Leg/Sector sit before the first date block.
FIXED_COLS = 2

#: The columns inside a date block are NOT the same on every sheet. 2024 and
#: 2025 repeat three -- Capacity, Flown, Load Factor -- while 2026 repeats
#: six, adding STD, Sold and Unsold. Reading a fixed six against a 2025 sheet
#: takes the next date's Capacity for this date's Sold and turns a real 55%
#: load factor into 1.4%, so the layout is read off each sheet instead.
_FIELD_ALIASES = {
    "capacity": "capacity",
    "flown": "flown",
    "std": "std",
    "sold": "sold",
    "unsold": "unsold",
    "loadfactor": "load_factor",
}

_DATE_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*(?:\(([A-Za-z]+)\))?")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")


def parse_header_date(text) -> tuple:
    """('03/02/2026 (Tuesday)') -> (date(2026, 2, 3), True).

    The second element says whether the weekday in the label agrees with the
    date read day-first. A disagreement means the sheet is month-first, or
    the label is wrong, and either way the caller should not quietly average
    it in with the rest.
    """
    m = _DATE_RE.match(str(text or ""))
    if not m:
        return None, False
    day, month, year, weekday = m.groups()
    try:
        got = date(int(year), int(month), int(day))
    except ValueError:
        return None, False
    if not weekday:
        return got, True
    named = weekday.strip().lower()
    agrees = (named in _WEEKDAYS
              and _WEEKDAYS[got.weekday()] == named)
    return got, agrees


def _num(value):
    """A number, or None. '#DIV/0!' and '' are not numbers."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text.startswith("#"):
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _clock(value) -> str:
    """'07:10' from whatever the cell holds, or ''."""
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%H:%M")
    text = str(value).strip()
    m = re.match(r"^(\d{1,2}):(\d{2})", text)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""


@dataclass
class Leg:
    """One flight on one date."""

    flight_date: date
    flight_no: str
    leg_route: str
    capacity: float | None = None
    flown: float | None = None
    #: Only the 2026 layout carries this; None on the earlier sheets.
    sold: float | None = None
    std: str = ""

    @property
    def origin(self) -> str:
        return self.leg_route.split("-", 1)[0] if "-" in self.leg_route else ""

    @property
    def destination(self) -> str:
        return self.leg_route.split("-", 1)[1] if "-" in self.leg_route else ""

    @property
    def load_factor(self):
        """Recomputed, never read from the sheet's own column.

        That column holds '#DIV/0!' wherever capacity was blank.
        """
        if not self.capacity or self.flown is None:
            return None
        return self.flown / self.capacity

    @property
    def operated(self) -> bool:
        return bool(self.capacity)


@dataclass
class LoadHistory:
    legs: list = field(default_factory=list)
    #: Date blocks skipped because a flight did not operate that day.
    not_operated: int = 0
    #: Date headers whose weekday contradicted the day-first reading.
    disputed_dates: list = field(default_factory=list)
    sheets: list = field(default_factory=list)
    #: Sheets whose block layout could not be read, rather than guessed at.
    unreadable_sheets: list = field(default_factory=list)

    @property
    def span(self) -> tuple:
        days = [x.flight_date for x in self.legs]
        return (min(days), max(days)) if days else (None, None)

    @property
    def routes(self) -> set:
        return {x.leg_route for x in self.legs}

    def for_route(self, route: str) -> list:
        want = route.upper()
        return [x for x in self.legs if x.leg_route.upper() == want]

    def summary(self) -> str:
        lo, hi = self.span
        return (f"{len(self.legs):,} operated flight-legs across "
                f"{len(self.routes)} route(s), "
                + (f"{lo} to {hi}" if lo else "no dates")
                + f"; {self.not_operated:,} block(s) held no flight")


def _block_layout(blocks, header_cols) -> dict:
    """Which offset inside a date block holds which figure, for THIS sheet.

    Derived from the sub-header row between one date block and the next,
    so a three-column year and a six-column year both read correctly, and
    a fourth layout would too.
    """
    if not blocks:
        return {}
    start = blocks[0][0]
    width = ((blocks[1][0] - start) if len(blocks) > 1
             else max(1, len(header_cols) - start))
    out = {}
    for k in range(width):
        col = start + k
        if col >= len(header_cols):
            break
        key = re.sub(r"[^a-z]", "", str(header_cols[col] or "").lower())
        name = _FIELD_ALIASES.get(key)
        if name and name not in out:
            out[name] = k
    return out


def parse_sheet(ws, history: LoadHistory) -> None:
    """Unpivot one year sheet into `history`."""
    rows = ws.iter_rows(values_only=True)
    try:
        header_dates = list(next(rows))
        header_cols = list(next(rows))
    except StopIteration:
        return
    # every date block starts where row 1 carries a date label
    blocks = []
    for i, cell in enumerate(header_dates):
        if i < FIXED_COLS or cell is None:
            continue
        when, agrees = parse_header_date(cell)
        if when is None:
            continue
        if not agrees:
            history.disputed_dates.append(str(cell))
        blocks.append((i, when))
    if not blocks:
        return
    history.sheets.append(getattr(ws, "title", ""))
    idx = _block_layout(blocks, header_cols)
    if "capacity" not in idx:
        history.unreadable_sheets.append(getattr(ws, "title", ""))
        return
    for row in rows:
        if not row or len(row) < FIXED_COLS:
            continue
        flight_no = str(row[0] or "").strip()
        leg_route = str(row[1] or "").strip().upper()
        if not flight_no or not leg_route:
            continue
        for start, when in blocks:
            def at(name, _start=start, _row=row):
                """A field of this date's block, by name, for this sheet."""
                off = idx.get(name)
                if off is None or _start + off >= len(_row):
                    return None
                return _row[_start + off]

            cap = _num(at("capacity"))
            if not cap:
                # no capacity means the flight did not operate: a blank, not
                # a zero-load day to average in
                history.not_operated += 1
                continue
            history.legs.append(Leg(
                flight_date=when, flight_no=flight_no, leg_route=leg_route,
                capacity=cap, flown=_num(at("flown")),
                sold=_num(at("sold")), std=_clock(at("std"))))


def read_workbook(path, sheets=None) -> LoadHistory:
    """Every year sheet in the load-analysis workbook."""
    from openpyxl import load_workbook

    history = LoadHistory()
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            title = str(getattr(ws, "title", "")).strip()
            if sheets is not None and title not in sheets:
                continue
            # the Summary sheet holds averages, not dated legs
            if not title.isdigit():
                continue
            parse_sheet(ws, history)
    finally:
        wb.close()
    history.legs.sort(key=lambda x: (x.flight_date, x.flight_no))
    return history
