"""Agency visit reports -> ONE master sheet.

Each sales rep keeps a sheet, and inside it a block per working day: a header
naming the date, the zone and the rep, then up to ten agencies visited that day
with the contact, the location, what that agency bills a month and the routes it
sells.

The parsing is the same shape as the counter reports and defensive for the same
reason: these are hand-kept files. The column set changes between blocks
("Productivity / Month" one day, "Productivity / Monthly Sale" the next, plain
"Monthly Sale" on a third), zones are written six different ways, and more than
a third of the productivity figures are simply never filled in. Nothing here
infers a value that was not written -- a blank is reported as a blank, because a
visit report that flatters itself is worth less than no report.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook

# --------------------------------------------------------------------------
# grammars
# --------------------------------------------------------------------------
# a block header carries at least two of these, whatever else it carries
HEADER_WORDS = ("name of agent", "contact person", "designation", "location")
SERIAL_RE = re.compile(r"^\d{1,3}$")
# a single definite figure, optionally in lakh or crore. A RANGE is not a
# figure: "3-5 lakh" is a conversation, not a number, and must not become 3.
MONEY_RE = re.compile(
    r"(?<![\d.])(?P<num>[\d,]+(?:\.\d+)?)\s*(?P<unit>lakh|lac|crore|cr|k)?",
    re.I)
RANGE_RE = re.compile(r"\d\s*(?:-|to|~)\s*\d", re.I)
UNITS = {"lakh": 100_000, "lac": 100_000, "crore": 10_000_000,
         "cr": 10_000_000, "k": 1_000}
MONTH_NAMES = {m.lower(): i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1)}
DAY_MONTH_RE = re.compile(
    r"(\d{1,2})\s*(?:st|nd|rd|th)?[\s,.-]+"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", re.I)
# a zone written as a bare code beside the date, e.g. "11B"
ZONE_CODE_RE = re.compile(r"^\d{1,2}[A-Za-z]?$")
PHONE_RE = re.compile(r"\d{6,}")
ZONE_RE = re.compile(r"zone\s*[#:]?\s*(\d+)", re.I)
DATE_RE = re.compile(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})")

# how the same column gets typed across blocks
COLUMNS = {
    "agency": ("name of agent", "agency name", "name of agency"),
    "contact": ("contact person", "contact name"),
    "designation": ("designation", "post"),
    "phone": ("contact no", "contact number", "mobile", "phone"),
    "location": ("location", "area"),
    "productivity": ("productivity / monthly sale", "productivity / month",
                     "productivity/month", "monthly sale", "productivity",
                     "monthly sales"),
    "routes": ("most selling route", "most selling routes", "selling route",
               "route"),
    "remarks": ("remarks", "discussion", "feedback", "comments"),
}


def _n(v) -> str:
    return re.sub(r"\s+", " ", str(v)).strip() if v is not None else ""


def _key(v) -> str:
    return _n(v).lower().rstrip(":.").strip()


def _column_of(header: str) -> str | None:
    k = _key(header)
    if not k:
        return None
    for field_name, spellings in COLUMNS.items():
        if k in spellings:
            return field_name
    for field_name, spellings in COLUMNS.items():   # tolerate a typo or a suffix
        if any(k.startswith(s[:8]) for s in spellings):
            return field_name
    return None


def parse_money(v):
    """The monthly billing an agency was credited with, or None if not written.

    Written as 'BDT- 45433558/-', 'BDT 0/-', '45,433,558' and other shapes. A
    zero is a real answer and is kept as zero; a blank is None, and the two are
    never merged -- 'we did not ask' is not 'they sell nothing'.
    """
    s = _n(v)
    if not s:
        return None
    if RANGE_RE.search(s):
        return None                     # "3-5 lakh" -- ask, do not guess
    # the hyphen in "BDT- 45433558/-" is punctuation, not a minus sign
    body = re.sub(r"(?i)(bdt|tk|taka)\s*[-:]?", " ", s)
    m = MONEY_RE.search(body)
    if not m or not m.group("num").strip(","):
        return None
    try:
        value = float(m.group("num").replace(",", ""))
    except ValueError:
        return None
    unit = (m.group("unit") or "").lower().strip()
    return abs(value) * UNITS.get(unit, 1)


def parse_visit_date(text: str, *, month=None, year=None):
    """The day a block covers. The caller's month wins on a stale template."""
    if isinstance(text, (datetime, date)):
        d = text.date() if isinstance(text, datetime) else text
        return d
    raw = _n(text)
    # "2 Aug, Sunday" and "03 Aug, Monday" carry no year, so the caller's does
    named = DAY_MONTH_RE.search(raw)
    if named and not DATE_RE.search(raw):
        try:
            return date(year or date.today().year,
                        MONTH_NAMES[named.group(2).lower()[:3]],
                        int(named.group(1)))
        except (ValueError, KeyError):
            return None
    m = DATE_RE.search(raw)
    if not m:
        return None
    d, mo, y = (int(x) for x in m.groups())
    if y < 100:
        y += 2000
    if d > 31 and mo <= 31:            # written the other way round
        d, mo = mo, d
    try:
        return date(y, mo, min(d, 28) if mo == 2 else d)
    except ValueError:
        return None


def parse_zone(text: str) -> str:
    """'Zone # 1', 'Zone: 9', 'Zone 26', 'Zone #16' -> 'Zone 1' … or ''."""
    m = ZONE_RE.search(_n(text))
    return f"Zone {int(m.group(1))}" if m else ""


@dataclass
class Visit:
    rep: str
    sheet: str
    day: date | None
    zone: str
    agency: str
    contact: str = ""
    designation: str = ""
    phone: str = ""
    location: str = ""
    productivity: float | None = None      # None means nobody wrote it
    routes: str = ""
    remarks: str = ""


@dataclass
class VisitIssue:
    rep: str
    kind: str
    detail: str = ""


@dataclass
class VisitData:
    visits: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    blocks: int = 0
    sheets: int = 0
    empty_sheets: int = 0
    files: list = field(default_factory=list)

    @property
    def days(self) -> set:
        return {v.day for v in self.visits if v.day}


# --------------------------------------------------------------------------
# reading a workbook
# --------------------------------------------------------------------------
def _is_block_header(keys) -> bool:
    joined = " ".join(keys)
    return sum(1 for w in HEADER_WORDS if w in joined) >= 2


def _read_context(cells, keys, current, raw=None):
    """Pick the date, zone and rep out of a block's preamble."""
    day, zone, rep = current
    joined = " ".join(keys)
    if "date & day" in joined or "date&day" in joined:
        for j, k in enumerate(keys):
            if "date" in k and j + 1 < len(cells) and cells[j + 1]:
                # prefer the raw cell: a real date is a datetime, not a string
                day = (raw[j + 1] if raw and j + 1 < len(raw)
                       and raw[j + 1] is not None else cells[j + 1])
    if "zone" in joined:
        for c in cells:
            z = parse_zone(c)
            if z:
                zone = z
    elif "date" in joined:
        # some reps write the zone as a bare code beside the date, e.g. "11B"
        for c in cells[1:]:
            if ZONE_CODE_RE.match(c):
                zone = f"Zone {c.upper()}"
                break
    for j, k in enumerate(keys):
        if k.startswith("name") and "agent" not in k and "agency" not in k:
            if j + 1 < len(cells) and cells[j + 1]:
                rep = cells[j + 1]
    return day, zone, rep


def parse_workbook(path, *, month=None, year=None) -> VisitData:
    """Every agency visit in one report file."""
    path = Path(path)
    data = VisitData(files=[path.name])
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            if not any(any(_n(v) for v in r if v is not None) for r in rows):
                data.empty_sheets += 1
                continue
            data.sheets += 1
            cols: dict[int, str] = {}
            raw_day = zone = rep = ""
            for row in rows:
                cells = [_n(c) for c in row]
                keys = [_key(c) for c in cells]
                if not any(cells):
                    continue
                if _is_block_header(keys):
                    # the columns are re-declared for every block, and they move
                    cols = {}
                    for j, k in enumerate(keys):
                        name = _column_of(k)
                        if name and j not in cols:
                            cols[j] = name
                    data.blocks += 1
                    continue
                raw_day, zone, rep = _read_context(cells, keys,
                                                   (raw_day, zone, rep),
                                                   raw=list(row))
                if not cols or not cells[0] or not SERIAL_RE.match(cells[0]):
                    continue

                got = {}
                for j, name in cols.items():
                    if j < len(cells) and cells[j]:
                        got[name] = cells[j]
                agency = got.get("agency", "")
                if not agency:
                    data.issues.append(VisitIssue(rep or ws.title, "no_agency",
                                                  f"{ws.title} row {cells[0]}"))
                    continue
                day = parse_visit_date(raw_day, month=month, year=year)
                if day is None:
                    data.issues.append(VisitIssue(rep or ws.title, "no_date",
                                                  agency))
                elif month and year and (day.month != month or day.year != year):
                    # An August report holding a March date is a template that
                    # was copied and not fully edited -- the same thing the
                    # counter sheets do. The DAY is what the rep meant; the
                    # month is the caller's, and the discrepancy is reported.
                    data.issues.append(VisitIssue(
                        rep or ws.title, "date_outside_month",
                        f"{agency}: written {day:%d %b %Y}"))
                    try:
                        day = date(year, month, day.day)
                    except ValueError:
                        day = None
                prod = parse_money(got.get("productivity"))
                if prod is None:
                    data.issues.append(VisitIssue(rep or ws.title,
                                                  "no_productivity", agency))
                if not got.get("phone"):
                    data.issues.append(VisitIssue(rep or ws.title, "no_phone",
                                                  agency))
                data.visits.append(Visit(
                    rep=rep or ws.title, sheet=ws.title, day=day, zone=zone,
                    agency=agency, contact=got.get("contact", ""),
                    designation=got.get("designation", ""),
                    phone=got.get("phone", ""), location=got.get("location", ""),
                    productivity=prod, routes=got.get("routes", ""),
                    remarks=got.get("remarks", "")))
    finally:
        wb.close()
    return data


def find_visit_reports(folder) -> list:
    return sorted(p for p in Path(folder).iterdir()
                  if p.suffix.lower() in (".xlsx", ".xlsm")
                  and not p.name.startswith("~$") and p.is_file())


def resolve_visit_inputs(selection) -> list:
    """A folder, a file, or any mix -- de-duplicated, as the counter side does."""
    if selection is None:
        return []
    if isinstance(selection, (str, Path)):
        text = str(selection).strip().strip('"')
        if not text:
            return []
        p = Path(text)
        if p.is_dir():
            return find_visit_reports(p)
        return [p] if (p.is_file() and p.suffix.lower() in (".xlsx", ".xlsm")
                       and not p.name.startswith("~$")) else []
    seen: dict = {}
    for item in selection:
        for p in resolve_visit_inputs(item):
            seen.setdefault(p.resolve(), p)
    return sorted(seen.values(), key=lambda x: x.name.lower())


def read_all(selection, *, month=None, year=None, progress_cb=None,
             stop_flag=None) -> VisitData:
    paths = resolve_visit_inputs(selection)
    if not paths:
        raise ValueError(f"No visit report workbooks found in {selection}")
    total = VisitData()
    for i, p in enumerate(paths, start=1):
        if stop_flag is not None and stop_flag():
            break
        if progress_cb is not None:
            progress_cb(i, len(paths), p.stem)
        one = parse_workbook(p, month=month, year=year)
        total.visits.extend(one.visits)
        total.issues.extend(one.issues)
        total.blocks += one.blocks
        total.sheets += one.sheets
        total.empty_sheets += one.empty_sheets
        total.files.extend(one.files)
    return total


# --------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------
@dataclass
class RepStats:
    rep: str
    visits: int = 0
    agencies: set = field(default_factory=set)
    repeats: int = 0
    days: set = field(default_factory=set)
    zones: set = field(default_factory=set)
    productivity: float = 0.0
    with_figure: int = 0
    no_phone: int = 0
    no_date: int = 0

    @property
    def per_day(self) -> float:
        return self.visits / len(self.days) if self.days else 0.0

    @property
    def figure_rate(self) -> float:
        return self.with_figure / self.visits if self.visits else 0.0


def summarise(data: VisitData):
    """Per rep, per zone, per agency -- counting blanks as blanks throughout."""
    reps: dict = {}
    seen: Counter = Counter()
    for v in data.visits:
        r = reps.get(v.rep)
        if r is None:
            r = reps[v.rep] = RepStats(rep=v.rep)
        key = (v.rep, v.agency.strip().upper())
        seen[key] += 1
        if seen[key] > 1:
            r.repeats += 1
        r.visits += 1
        r.agencies.add(v.agency.strip().upper())
        if v.day:
            r.days.add(v.day)
        else:
            r.no_date += 1
        if v.zone:
            r.zones.add(v.zone)
        if v.productivity is not None:
            r.productivity += v.productivity
            r.with_figure += 1
        if not v.phone:
            r.no_phone += 1

    zones: dict = defaultdict(lambda: Counter())
    for v in data.visits:
        z = zones[v.zone or "(zone not written)"]
        z["visits"] += 1
        z["agencies"] = 0            # filled below, needs a set
    zone_agencies: dict = defaultdict(set)
    zone_reps: dict = defaultdict(set)
    for v in data.visits:
        zone_agencies[v.zone or "(zone not written)"].add(v.agency.upper())
        zone_reps[v.zone or "(zone not written)"].add(v.rep)
    for z, c in zones.items():
        c["agencies"] = len(zone_agencies[z])
        c["reps"] = len(zone_reps[z])

    agencies: dict = {}
    for v in data.visits:
        key = v.agency.strip().upper()
        a = agencies.setdefault(key, {"name": v.agency.strip(), "visits": 0,
                                      "reps": set(), "productivity": None,
                                      "location": v.location, "routes": ""})
        a["visits"] += 1
        a["reps"].add(v.rep)
        if v.productivity is not None:
            # the largest figure any rep recorded for this agency
            a["productivity"] = max(a["productivity"] or 0, v.productivity)
        if v.routes and not a["routes"]:
            a["routes"] = v.routes
    return reps, dict(zones), agencies


ROUTE_SPLIT = re.compile(r"[,/;&+]| and ", re.I)


def route_demand(data: VisitData) -> Counter:
    """Which sectors the visited agencies say they sell most."""
    out: Counter = Counter()
    for v in data.visits:
        for part in ROUTE_SPLIT.split(v.routes or ""):
            token = re.sub(r"[^A-Za-z ]", "", part).strip()
            if 2 <= len(token) <= 24 and token.lower() not in (
                    "and", "all", "etc", "sector", "domistic route"):
                out[token.title()] += 1
    return out


# --------------------------------------------------------------------------
# the master sheet
# --------------------------------------------------------------------------
@dataclass
class VisitResult:
    path: Path
    visits: int = 0
    reps: int = 0
    agencies: int = 0
    days: int = 0
    repeats: int = 0
    figure_rate: float = 0.0
    productivity: float = 0.0
    zone_blank: int = 0
    stopped: bool = False
    window: str = ""


def build_master(selection, out_path, *, month: int, year: int,
                 progress_cb=None, stop_flag=None) -> VisitResult:
    """Read every visit report and write ONE master sheet."""
    from .counter_master import (BAD, BAND, COLS, GOOD, GREY, LAST, MONEY,
                                 NAVY, PAPER, PCT, WARN, _band, _cell,
                                 _headers, _kpi_strip)
    from openpyxl import Workbook
    from openpyxl.formatting.rule import ColorScaleRule, DataBarRule

    data = read_all(selection, month=month, year=year, progress_cb=progress_cb,
                    stop_flag=stop_flag)
    if not data.visits:
        raise ValueError("No agency visits were found in those files.")
    reps, zones, agencies = summarise(data)
    issues = Counter(i.kind for i in data.issues)

    wb = Workbook()
    ws = wb.active
    ws.title = "Visits"
    ws.sheet_view.showGridLines = False
    for col, width in COLS:
        ws.column_dimensions[col].width = width

    days = sorted(data.days)
    window = (f"{days[0]:%d %b} to {days[-1]:%d %b %Y}" if days else "no dates")
    with_figure = sum(1 for v in data.visits if v.productivity is not None)
    total_prod = sum(v.productivity for v in data.visits
                     if v.productivity is not None)
    repeats = sum(r.repeats for r in reps.values())
    zone_blank = sum(1 for v in data.visits if not v.zone)

    ws.merge_cells(f"A1:{LAST}1")
    _cell(ws, 1, 1, f"  AGENCY VISITS — MASTER SUMMARY   ·   "
                    f"{date(year, month, 1):%B %Y}",
          bold=True, size=16, color="FFFFFF", fill=NAVY, align="left")
    ws.row_dimensions[1].height = 34
    ws.merge_cells(f"A2:{LAST}2")
    _cell(ws, 2, 1,
          f"  {len(reps)} reps · {len(data.visits):,} visits · "
          f"{len(agencies):,} agencies · {window} · "
          f"{len(data.files)} file(s), {data.blocks} daily blocks."
          "   A figure the rep did not write is counted as not written, never "
          "as zero — and a range like '3-5 lakh' is a conversation, not a "
          "number, so it is not read as one.",
          size=9, color=GREY, fill=PAPER, align="left")
    ws.row_dimensions[2].height = 17

    r = 4
    r = _band(ws, r, "AT A GLANCE")
    r = _kpi_strip(ws, r, [
        ("Sales reps", len(reps), "0", None),
        ("Visits logged", len(data.visits), "#,##0", None),
        ("Agencies seen", len(agencies), "#,##0", None),
        ("Days covered", len(days), "0", None),
        ("Repeat visits", repeats, "#,##0", None),
        ("Visits per rep per day",
         len(data.visits) / max(sum(len(x.days) for x in reps.values()), 1),
         "0.0", None),
        ("Zones covered", len([z for z in zones if "not written" not in z]),
         "0", None),
    ])
    r = _kpi_strip(ws, r, [
        ("Monthly billing seen", total_prod, MONEY, None),
        ("Visits WITH a figure", with_figure, "#,##0", None),
        ("Figure not written", len(data.visits) - with_figure, "#,##0",
         "C00000"),
        ("Figure coverage", with_figure / len(data.visits), PCT, None),
        ("Zone not written", zone_blank, "#,##0", "C00000"),
        ("Date not written", issues.get("no_date", 0), "#,##0", "C00000"),
        ("Date outside the month", issues.get("date_outside_month", 0),
         "#,##0", "C00000"),
    ])
    r += 1

    # ---- reps ------------------------------------------------------------
    r = _band(ws, r, "SALES REP SCORECARD",
              "ranked by agencies actually seen, not by visits logged — "
              "revisiting one agency ten times is not ten agencies")
    first = r
    r = _headers(ws, r, [
        "Sales rep", "Visits", "Agencies seen", "Repeat visits", "Repeat %",
        "Days out", "Visits per day", "Zones", "Billing seen (BDT)",
        "Figure written", "Figure coverage", "Phone missing", "Date missing",
        "", "", "", "", "", "", "", "Verdict"])
    for rp in sorted(reps.values(), key=lambda x: -len(x.agencies)):
        cover = rp.figure_rate
        if rp.visits and len(rp.agencies) / rp.visits < 0.5:
            verdict, tone = "MOSTLY REVISITS", WARN
        elif cover < 0.25:
            verdict, tone = "FIGURES NOT WRITTEN", WARN
        elif rp.per_day >= 8 and cover >= 0.5:
            verdict, tone = "STRONG", GOOD
        else:
            verdict, tone = "OK", None
        _cell(ws, r, 1, rp.rep, bold=True, size=10, border=True)
        _cell(ws, r, 2, rp.visits, size=9, border=True, align="center")
        _cell(ws, r, 3, len(rp.agencies), bold=True, size=10, border=True,
              align="center")
        _cell(ws, r, 4, rp.repeats or None, size=9, border=True, align="center")
        _cell(ws, r, 5, rp.repeats / rp.visits if rp.visits else None, fmt=PCT,
              size=9, border=True, align="center")
        _cell(ws, r, 6, len(rp.days) or None, size=9, border=True,
              align="center")
        _cell(ws, r, 7, rp.per_day or None, fmt="0.0", size=9, border=True,
              align="center")
        _cell(ws, r, 8, len(rp.zones) or None, size=9, border=True,
              align="center")
        _cell(ws, r, 9, rp.productivity or None, fmt=MONEY, size=9, border=True,
              align="right")
        _cell(ws, r, 10, rp.with_figure or None, size=9, border=True,
              align="center")
        _cell(ws, r, 11, cover, fmt=PCT, size=9, border=True, align="center",
              fill=BAD if cover < 0.25 else None)
        _cell(ws, r, 12, rp.no_phone or None, size=9, border=True,
              align="center")
        _cell(ws, r, 13, rp.no_date or None, size=9, border=True,
              align="center")
        for j in range(14, 21):
            _cell(ws, r, j, None, border=True)
        _cell(ws, r, 21, verdict, bold=True, size=9, fill=tone, border=True,
              align="center")
        r += 1
    if r - 1 >= first:
        ws.conditional_formatting.add(
            f"C{first + 1}:C{r - 1}",
            DataBarRule(start_type="num", start_value=0, end_type="max",
                        color=BAND, showValue=True))
        ws.conditional_formatting.add(
            f"K{first + 1}:K{r - 1}",
            ColorScaleRule(start_type="num", start_value=0, start_color=BAD,
                           mid_type="num", mid_value=0.5, mid_color=WARN,
                           end_type="num", end_value=1, end_color=GOOD))
    r += 1

    # ---- zones -----------------------------------------------------------
    r = _band(ws, r, "ZONE COVERAGE",
              "a zone nobody wrote down cannot be assigned to anyone")
    r = _headers(ws, r, ["Zone", "Visits", "Agencies", "Reps working it",
                         "", "", "", "", "", "", "", "", "", "", "", "", "",
                         "", "", "", ""])
    for z, c in sorted(zones.items(), key=lambda kv: -kv[1]["visits"]):
        blank = "not written" in z
        _cell(ws, r, 1, z, bold=True, size=10, border=True,
              fill=BAD if blank else None)
        _cell(ws, r, 2, c["visits"], size=9, border=True, align="center")
        _cell(ws, r, 3, c["agencies"], size=9, border=True, align="center")
        _cell(ws, r, 4, c["reps"], size=9, border=True, align="center")
        for j in range(5, 22):
            _cell(ws, r, j, None, border=True)
        r += 1
    r += 1

    # ---- agencies --------------------------------------------------------
    r = _band(ws, r, "AGENCIES BY BILLING SEEN",
              "the largest monthly figure any rep recorded, and how often the "
              "agency was actually visited")
    hdr = r
    r = _headers(ws, r, ["Agency", "Monthly billing (BDT)", "Visits", "Reps",
                         "Location", "Most selling routes", "", "", "", "",
                         "", "", "", "", "", "", "", "", "", "", ""])
    ranked = sorted(agencies.values(),
                    key=lambda a: -(a["productivity"] or -1))[:60]
    for a in ranked:
        _cell(ws, r, 1, a["name"], bold=True, size=10, border=True)
        _cell(ws, r, 2, a["productivity"], fmt=MONEY, size=10, border=True,
              align="right")
        _cell(ws, r, 3, a["visits"], size=9, border=True, align="center")
        _cell(ws, r, 4, len(a["reps"]), size=9, border=True, align="center")
        _cell(ws, r, 5, a["location"][:28], size=9, border=True)
        _cell(ws, r, 6, a["routes"][:40], size=8, color=GREY, border=True)
        for j in range(7, 22):
            _cell(ws, r, j, None, border=True)
        r += 1
    if ranked:
        ws.conditional_formatting.add(
            f"B{hdr + 1}:B{r - 1}",
            DataBarRule(start_type="num", start_value=0, end_type="max",
                        color=BAND, showValue=True))
    r += 1

    # ---- routes ----------------------------------------------------------
    demand = route_demand(data)
    r = _band(ws, r, "WHAT THE AGENCIES SAY THEY SELL",
              "counted from the routes the reps wrote down, so it reflects the "
              "agencies visited rather than the market")
    r = _headers(ws, r, ["Sector", "Mentions", "Share of visits", "", "", "",
                         "", "", "", "", "", "", "", "", "", "", "", "", "",
                         "", ""])
    for sector, n in demand.most_common(20):
        _cell(ws, r, 1, sector, bold=True, size=10, border=True)
        _cell(ws, r, 2, n, size=9, border=True, align="center")
        _cell(ws, r, 3, n / len(data.visits), fmt=PCT, size=9, border=True,
              align="center")
        for j in range(4, 22):
            _cell(ws, r, j, None, border=True)
        r += 1
    r += 1

    # ---- data quality ----------------------------------------------------
    r = _band(ws, r, "DATA QUALITY — what the reports do not say",
              "every figure above is only as good as the column it came from")
    r = _headers(ws, r, ["What is missing", "Count", "Share of visits",
                         "Why it matters", "", "", "", "", "", "", "", "",
                         "", "", "", "", "", "", "", "", ""])
    why = {
        "no_productivity": "no billing figure, so the agency cannot be sized",
        "no_date": "the visit cannot be placed on a day",
        "date_outside_month": "written date is in another month; the day was "
                              "kept and the month taken from this report",
        "no_phone": "no contact number recorded",
        "no_agency": "a numbered row with no agency name — skipped entirely",
    }
    for kind, n in issues.most_common():
        _cell(ws, r, 1, kind.replace("_", " "), bold=True, size=10, border=True)
        _cell(ws, r, 2, n, size=9, border=True, align="center")
        _cell(ws, r, 3, n / len(data.visits), fmt=PCT, size=9, border=True,
              align="center", fill=BAD if n > len(data.visits) / 2 else None)
        ws.merge_cells(start_row=r, start_column=4, end_row=r, end_column=21)
        _cell(ws, r, 4, why.get(kind, ""), size=9, border=True)
        r += 1

    r += 1
    ws.merge_cells(f"A{r}:{LAST}{r}")
    _cell(ws, r, 1,
          "  Method: each rep's sheet repeats a block per day, and the columns "
          "are re-declared inside every block, so they are read per block "
          "rather than once per sheet. A date written in another month keeps "
          "its day and takes the month from this report, the same rule the "
          "counter reports use. Nothing is inferred: a blank stays blank.",
          size=8, color=GREY, fill=PAPER, wrap=True)
    ws.row_dimensions[r].height = 26

    ws.freeze_panes = "A9"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.orientation = "landscape"
    out_path = Path(out_path)
    wb.save(out_path)
    return VisitResult(
        path=out_path, visits=len(data.visits), reps=len(reps),
        agencies=len(agencies), days=len(days), repeats=repeats,
        figure_rate=with_figure / len(data.visits), productivity=total_prod,
        zone_blank=zone_blank, window=window,
        stopped=bool(stop_flag is not None and stop_flag()))


def build_master_path(out_dir, month: int, year: int) -> Path:
    return Path(out_dir) / f"Agency_Visits_{date(year, month, 1):%b%Y}.xlsx"
