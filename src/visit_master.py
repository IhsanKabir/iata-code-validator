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
from datetime import date, datetime, timedelta
from pathlib import Path

from openpyxl import load_workbook

# --------------------------------------------------------------------------
# grammars
# --------------------------------------------------------------------------
# a block header carries at least two of these, whatever else it carries
HEADER_WORDS = ("name of agent", "contact person", "designation", "location")
# Past this share of dates written outside the chosen month, the month is
# wrong rather than the dates: a run given September against the August
# reports moved 2,519 visits and printed a window nobody worked.
WRONG_MONTH = 0.5

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

# words that say what KIND of business an agency is, not which one it is
# Words that carry no identity at all: a legal form, a filing tag, a branch
# marker. These are dropped.
_AGENCY_NOISE = re.compile(
    r"\b(limited|ltd|ltdd|pvt|private|co|company|corp|corporation|inc|"
    r"m/s|ms|iata|non|the|and)\b|\((?:[a-z]{3}|iata|non[ -]?iata)\)", re.I)
# Words that ARE part of the name but get typed either way round. These are
# normalised, never deleted -- deleting them merged three different Sheba
# agencies (AIR SERVICE, AIR TRAVELS, TRAVELS & TOURS) into one.
_AGENCY_PLURAL = re.compile(
    r"\b(travel|tour|holiday|service|agency|agencie|enterprise)s?\b", re.I)


def identity(name: str) -> str:
    """A key that survives how the name was typed, without merging businesses.

    'AB Travel' and 'AB Travels' are one agency written twice, and the sales
    system files 'Carnival Air Ticketing Ltd. (IATA)' where a rep writes
    'CARNIVAL AIR TICKETING LTD.'. But 'SHEBA AIR SERVICE' and 'SHEBA TRAVELS &
    TOURS' are two businesses, so the trade words are folded to a single form
    rather than thrown away.
    """
    text = _n(name).replace("&", " and ")
    text = _AGENCY_NOISE.sub(" ", text)
    text = _AGENCY_PLURAL.sub(lambda m: m.group(1).lower(), text)
    return re.sub(r"[^a-z0-9]", "", text.lower())


def person_key(name: str) -> str:
    """'Md. Barru Ibna Azam' and 'Md Barru Ibna Azam Barno' are one rep."""
    return re.sub(r"[^a-z ]", "", _n(name).lower()).strip()


def canonical_people(names) -> dict:
    """Map every spelling of a rep to one name -- the fullest one written.

    One sheet carried three spellings of the same person, which split their work
    three ways in a scorecard meant to compare people.
    """
    keys = sorted({person_key(n): n for n in names}.items(),
                  key=lambda kv: -len(kv[0]))
    canon: dict[str, str] = {}
    chosen: list[tuple[str, str]] = []
    for key, name in keys:
        for other_key, other_name in chosen:
            if other_key.startswith(key) or key.startswith(other_key):
                canon[name] = other_name
                break
        else:
            chosen.append((key, name))
            canon[name] = name
    return canon
PHONE_RE = re.compile(r"\d{6,}")
ZONE_RE = re.compile(r"zone\s*[#:]?\s*(\d+)", re.I)
#: An ISO date, optionally with a time: what openpyxl hands over when a
#: real date cell is stringified before it reaches the parser.
ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T]|$)")
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
    # A real date cell that reached us as TEXT: '2026-09-01 00:00:00'. Read
    # right to left as day-month-year it becomes 26 September 2001 -- the last
    # two digits of the year taken for the day -- which then reads as a date
    # from another month and collapses a rep's whole month onto the 26th.
    iso = ISO_DATE_RE.match(raw)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)),
                        int(iso.group(3)))
        except ValueError:
            return None
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
    date_moved: bool = False   # the rep wrote another month; we moved the day


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
    duplicates: int = 0
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
                    # The blank forms carry pre-printed serials, so an
                    # untouched line looks exactly like a row somebody
                    # wrote and lost. It is not one: nothing was written,
                    # and counting it makes a rep who filled 8 of 18 lines
                    # look like they mislaid 10.
                    kind = ("no_agency" if any(got.values())
                            else "blank_row")
                    data.issues.append(VisitIssue(
                        rep or ws.title, kind,
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
                        day, moved = date(year, month, day.day), True
                    except ValueError:
                        day, moved = None, False
                else:
                    moved = False
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
                    remarks=got.get("remarks", ""), date_moved=moved))
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



def detect_period(selection, *, max_blocks: int = 4000):
    """The month these visit reports are for, by majority vote of their dates.

    The counter tab has read its month off the sheets since the day it was
    asked to; this tab never did, and defaulted to today's. Run on the 3rd of
    September against the August reports, that relabelled 2,519 August visits
    as September and printed "01 Sep to 29 Sep 2026" across the top -- a window
    that never existed.

    A vote, not a first match: a rep who writes last month's date on one block
    must not decide the month for everyone. Returns
    (month, year, votes, agreement), or (None, None, ...) when the files carry
    no readable date at all.
    """
    votes: Counter = Counter()
    seen = 0
    for path in resolve_visit_inputs(selection):
        try:
            wb = load_workbook(path, read_only=True, data_only=True)
        except Exception:                      # noqa: BLE001 - unreadable file
            continue                           # counted nowhere; the caller sees it
        try:
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    cells = [_n(c) for c in row]
                    keys = [_key(c) for c in cells]
                    joined = " ".join(keys)
                    if "date" not in joined:
                        continue
                    for j, k in enumerate(keys):
                        if "date" not in k or j + 1 >= len(cells):
                            continue
                        raw = (row[j + 1] if row[j + 1] is not None
                               else cells[j + 1])
                        day = parse_visit_date(raw)
                        if day is not None:
                            votes[(day.month, day.year)] += 1
                            seen += 1
                        break
                    if seen >= max_blocks:
                        break
                if seen >= max_blocks:
                    break
        finally:
            wb.close()
    if not votes:
        return None, None, votes, 0.0
    (month, year), hits = votes.most_common(1)[0]
    return month, year, votes, hits / sum(votes.values())


def read_all(selection, *, month=None, year=None, progress_cb=None,
             stop_flag=None) -> VisitData:
    paths = resolve_visit_inputs(selection)
    if not paths:
        raise ValueError(f"No visit report workbooks found in {selection}")
    total = VisitData()
    seen: set = set()
    for i, p in enumerate(paths, start=1):
        if stop_flag is not None and stop_flag():
            break
        if progress_cb is not None:
            progress_cb(i, len(paths), p.stem)
        one = parse_workbook(p, month=month, year=year)
        # A Downloads folder routinely holds "report.xlsx" and "report (1).xlsx".
        # Reading both doubled every figure -- 2,201 visits became 4,944 -- so a
        # visit already seen is dropped and counted, not added again.
        for v in one.visits:
            key = (person_key(v.rep), v.day, identity(v.agency), v.phone)
            if key in seen:
                total.duplicates += 1
                # Recorded against the rep, not just tallied. A dropped row is
                # an input the rep wrote that did not reach their Days out,
                # and "why not" is only answerable if we keep who and what.
                total.issues.append(VisitIssue(
                    v.rep, "duplicate",
                    f"{v.agency}"
                    + (f" on {v.day:%d %b}" if v.day else " (no date)")
                    + f" — also in {p.stem}"))
                continue
            seen.add(key)
            total.visits.append(v)
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
    canon = canonical_people({v.rep for v in data.visits})
    for v in data.visits:
        v.rep = canon.get(v.rep, v.rep)

    reps: dict = {}
    seen: Counter = Counter()
    for v in data.visits:
        r = reps.get(v.rep)
        if r is None:
            r = reps[v.rep] = RepStats(rep=v.rep)
        key = (v.rep, identity(v.agency))
        seen[key] += 1
        if seen[key] > 1:
            r.repeats += 1
        r.visits += 1
        r.agencies.add(identity(v.agency))
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
        key = identity(v.agency)
        a = agencies.setdefault(key, {"name": v.agency.strip(), "visits": 0,
                                      "reps": set(), "productivity": None,
                                      "figures": set(), "location": v.location,
                                      "routes": "", "spellings": set()})
        a["visits"] += 1
        a["reps"].add(v.rep)
        a["spellings"].add(v.agency.strip())
        if v.productivity is not None:
            a["figures"].add(v.productivity)
            a["productivity"] = max(a["productivity"] or 0, v.productivity)
        if v.routes and not a["routes"]:
            a["routes"] = v.routes
    for a in agencies.values():
        # 68 agencies were given two different figures, up to seven times
        # apart. Showing only the larger would hide the disagreement.
        a["disputed"] = len(a["figures"]) > 1
        if a["spellings"]:
            a["name"] = max(a["spellings"], key=len)
    return reps, dict(zones), agencies


# a route is written as codes joined any number of ways: "SPD-DAC-SPD",
# "KUL, SIN", "MCT/BD/MCT". Splitting on punctuation but NOT the hyphen turned
# SPD-DAC-SPD into the single token "Spddacspd", 201 times.
ROUTE_SPLIT = re.compile(r"[,/;&+\-\u2013\u2014]|\band\b|\bto\b", re.I)
# written sectors, and what to call each of them. Reps write "Domistic",
# "Middle East all sector." and similar, so the phrase is looked for inside the
# text rather than matched whole.
_WORD_SECTORS = {
    "domistic": "Domestic", "domestic": "Domestic",
    "middle east": "Middle East", "international": "International",
    "india": "India", "pakistan": "Pakistan", "nepal": "Nepal",
    "bangladesh": "Bangladesh", "saudi": "Saudi",
}


def route_demand(data: VisitData) -> Counter:
    """Which sectors the visited agencies say they sell most.

    Three-letter airport codes are counted as codes; a handful of written words
    ("Domestic", "Middle East") are counted as themselves. Anything else is left
    out rather than guessed at.
    """
    out: Counter = Counter()
    for v in data.visits:
        for part in ROUTE_SPLIT.split(v.routes or ""):
            token = re.sub(r"[^A-Za-z ]", " ", part).strip()
            token = re.sub(r"\s+", " ", token)
            if not token:
                continue
            low = token.lower()
            if len(token) == 3 and " " not in token:
                out[token.upper()] += 1
            else:
                for phrase, sector in _WORD_SECTORS.items():
                    if phrase in low:
                        out[sector] += 1
                        break
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
    duplicates: int = 0
    disputed: int = 0
    matched: int = 0
    lift: float | None = None
    control_change: float | None = None
    visited_change: float | None = None
    stopped: bool = False
    window: str = ""


def build_master(selection, out_path, *, month: int, year: int,
                 progress_cb=None, stop_flag=None,
                 use_warehouse: bool = False) -> VisitResult:
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

    # When nearly every written date belongs to another month, this is not a
    # handful of stale templates -- it is the wrong month, and relabelling
    # 2,519 August visits as September printed a window that never existed.
    # Say so where nobody can miss it, and name the month the dates point to.
    # Counted off the visits that survived, not the issues raised while
    # parsing: those are logged before duplicates are dropped, and dividing one
    # by the other reported 104% of the dates as wrong.
    dated = [v for v in data.visits if v.day]
    outside = sum(1 for v in dated if v.date_moved)
    share = outside / len(dated) if dated else 0.0
    really: tuple | None = None
    if share >= WRONG_MONTH:
        votes = Counter((i.detail or "")[-8:] for i in data.issues
                        if i.kind == "date_outside_month")
        really = votes.most_common(1)[0][0].strip() if votes else None

    ws.merge_cells(f"A1:{LAST}1")
    _cell(ws, 1, 1, f"  AGENCY VISITS — MASTER SUMMARY   ·   "
                    f"{date(year, month, 1):%B %Y}"
                    + (f"   ·   CHECK THE MONTH — {share:.0%} of the dates "
                       f"written are not in it"
                       + (f", most say {really}" if really else "")
                       if really else ""),
          bold=True, size=16, color="FFFFFF",
          fill="C00000" if really else NAVY, align="left")
    ws.row_dimensions[1].height = 34
    if really:
        ws.merge_cells(f"A3:{LAST}3")
        _cell(ws, 3, 1,
              f"  These visits were read as {date(year, month, 1):%B %Y} "
              f"because that is the month the run was given. The reps wrote "
              f"another one on {share:.0%} of them, so every date below was "
              f"moved into {date(year, month, 1):%B} and the window across the "
              f"top is not a window anyone worked. Set the month to what the "
              f"reports actually say and run it again.",
              size=10, bold=True, color="C00000", fill=PAPER, wrap=True)
        ws.row_dimensions[3].height = 30
    ws.merge_cells(f"A2:{LAST}2")
    _cell(ws, 2, 1,
          f"  {len(reps)} reps · {len(data.visits):,} visits · "
          f"{len(agencies):,} agencies · {window} · "
          f"{len(data.files)} file(s), {data.blocks} daily blocks"
          + (f", {data.duplicates:,} duplicate visit(s) dropped"
             if data.duplicates else "") + "."
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
        ("Duplicate visits dropped", data.duplicates, "#,##0",
         "C00000" if data.duplicates else None),
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
        if rp.no_date > rp.visits * 0.25:
            verdict, tone = "DATES MISSING", WARN
        elif rp.visits and len(rp.agencies) / rp.visits < 0.5:
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
        # a rep whose dates are mostly missing collapses onto one day, and
        # then "41 visits a day" is an artefact of the blanks, not a workload
        _shaky = rp.no_date > rp.visits * 0.25
        _cell(ws, r, 7, rp.per_day or None, fmt="0.0", size=9, border=True,
              align="center", fill=WARN if _shaky else None)
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
    # Days out counts DATES, not rows. The reconciliation sits here,
    # immediately under the column it explains, rather than on a sheet
    # of its own that nobody opens.
    r = write_day_audit(ws, r, data)

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
    r = _headers(ws, r, ["Agency", "Monthly billing (BDT)", "Reps disagree",
                         "Visits", "Reps", "Location", "Most selling routes",
                         "", "", "", "", "", "", "", "", "", "", "", "", "",
                         ""])
    ranked = sorted(agencies.values(),
                    key=lambda a: -(a["productivity"] or -1))[:60]
    for a in ranked:
        _cell(ws, r, 1, a["name"], bold=True, size=10, border=True)
        _cell(ws, r, 2, a["productivity"], fmt=MONEY, size=10, border=True,
              align="right")
        _cell(ws, r, 3,
              (f"{min(a['figures']):,.0f} … {max(a['figures']):,.0f}"
               if a["disputed"] else None),
              size=8, color="C00000", border=True,
              fill=WARN if a["disputed"] else None)
        _cell(ws, r, 4, a["visits"], size=9, border=True, align="center")
        _cell(ws, r, 5, len(a["reps"]), size=9, border=True, align="center")
        _cell(ws, r, 6, a["location"][:28], size=9, border=True)
        _cell(ws, r, 7, a["routes"][:40], size=8, color=GREY, border=True)
        for j in range(8, 22):
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

    impact = None
    if use_warehouse and not (stop_flag is not None and stop_flag()):
        # a second sheet: the visits only mean something next to what the
        # agencies actually bought
        from . import counter_reconcile as cr
        src = cr.find_sales_warehouse()
        if src is None:
            raise ValueError(
                "No sales data found on this machine, so visit impact cannot "
                "be measured. Untick the box, or point ANALYSIS_HOME at the "
                "data root.")
        rows = read_sales_for_impact(
            src, month=month, year=year,
            progress_cb=(lambda n: progress_cb(0, 0, f"sales data: {n:,} rows"))
            if progress_cb else None)
        impact = measure_impact(data, rows, month=month, year=year)
        write_impact(wb.create_sheet("Impact"), impact, month=month, year=year)

    out_path = Path(out_path)
    wb.save(out_path)
    return VisitResult(
        path=out_path, visits=len(data.visits), reps=len(reps),
        agencies=len(agencies), days=len(days), repeats=repeats,
        figure_rate=with_figure / len(data.visits), productivity=total_prod,
        zone_blank=zone_blank, window=window, duplicates=data.duplicates,
        disputed=sum(1 for a in agencies.values() if a["disputed"]),
        matched=impact.matched if impact else 0,
        lift=impact.lift if impact else None,
        control_change=impact.control_change if impact else None,
        visited_change=impact.visited_change if impact else None,
        stopped=bool(stop_flag is not None and stop_flag()))


def build_master_path(out_dir, month: int, year: int) -> Path:
    return Path(out_dir) / f"Agency_Visits_{date(year, month, 1):%b%Y}.xlsx"


# --------------------------------------------------------------------------
# did the visits change anything?
# --------------------------------------------------------------------------
EARLY_DAY = 10           # a visit by this day precedes most of the window
GROWTH_BAND = 0.05       # inside this, call it flat rather than a move
# A near match is only allowed on the DISTINCTIVE part of the name. Matching
# whole names let "Tamchi Tours & Travels" score 92 against "Tamim Tours &
# Travels", because the shared boilerplate carried it. Below this length a key
# is too short to be sure of.
NEAR_SCORE = 92
NEAR_MIN_KEY = 6


@dataclass
class AgencyImpact:
    key: str
    name: str
    rep: str
    visits: int
    first_visit: date | None
    before: float = 0.0
    after: float = 0.0
    matched: bool = False
    match_kind: str = ""          # "exact" | "near"
    matched_as: str = ""          # the customer name it was matched to

    @property
    def change(self):
        if not self.matched or not self.before:
            return None
        return (self.after - self.before) / self.before

    @property
    def verdict(self) -> str:
        if not self.matched:
            return "no sales found"
        if not self.before and self.after:
            return "started buying"
        if self.before and not self.after:
            return "stopped buying"
        c = self.change
        if c is None:
            return "no sales either side"
        return ("grew" if c > GROWTH_BAND
                else "fell" if c < -GROWTH_BAND else "flat")


@dataclass
class ImpactResult:
    """Visited against never-visited, over the same days.

    The comparison is the whole point. Visited agencies fell 5.9% over August,
    which reads as a failure until you see that agencies nobody visited fell
    8.0% over exactly the same days.
    """
    agencies: list = field(default_factory=list)
    visited_before: float = 0.0
    visited_after: float = 0.0
    control_before: float = 0.0
    control_after: float = 0.0
    control_agencies: int = 0
    early_before: float = 0.0
    early_after: float = 0.0
    late_before: float = 0.0
    late_after: float = 0.0
    unvisited_top: list = field(default_factory=list)
    window: str = ""
    # The headline moves with choices nobody outside this code can see, so all
    # of them are carried and printed together rather than one being picked.
    # Restricting both sides to agencies that bought in the BEFORE window is
    # the like-for-like basis: the control is otherwise flattered by agencies
    # that had no July sales at all and could only go up.
    trading_visited: tuple = (0.0, 0.0)
    trading_control: tuple = (0.0, 0.0)
    sized_control: tuple = (0.0, 0.0)
    sized_control_n: int = 0
    exact_visited: tuple = (0.0, 0.0)
    started_visited: int = 0
    started_control: int = 0

    @staticmethod
    def _rate(before, after):
        return (after - before) / before if before else None

    @property
    def visited_change(self):
        return self._rate(self.visited_before, self.visited_after)

    @property
    def control_change(self):
        return self._rate(self.control_before, self.control_after)

    @property
    def lift(self):
        """Percentage points the visited agencies beat the control by."""
        v, c = self.visited_change, self.control_change
        return None if v is None or c is None else v - c

    @property
    def early_lift(self):
        v = self._rate(self.early_before, self.early_after)
        c = self.control_change
        return None if v is None or c is None else v - c

    @property
    def late_lift(self):
        v = self._rate(self.late_before, self.late_after)
        c = self.control_change
        return None if v is None or c is None else v - c

    @property
    def matched(self) -> int:
        return sum(1 for a in self.agencies if a.matched)

    @staticmethod
    def _lift(v, c):
        vr = ImpactResult._rate(*v)
        cr = ImpactResult._rate(*c)
        return None if vr is None or cr is None else vr - cr

    @property
    def trading_lift(self):
        """The like-for-like answer: both sides already trading in July."""
        return self._lift(self.trading_visited, self.trading_control)

    @property
    def sized_lift(self):
        """Against a control of comparable size, not the whole long tail."""
        return self._lift(self.trading_visited, self.sized_control)

    @property
    def exact_lift(self):
        """Ignoring every near match, in case one of them is wrong."""
        return self._lift(self.exact_visited, self.trading_control)

    @property
    def lift_range(self):
        vals = [x for x in (self.lift, self.trading_lift, self.sized_lift,
                            self.exact_lift) if x is not None]
        return (min(vals), max(vals)) if vals else (None, None)


def _near_matches(unmatched, totals) -> dict:
    """Visited agencies whose customer record is spelled slightly differently.

    Only the distinctive part is compared -- what is left after the words that
    say what KIND of business it is -- so "secquence" finds "sequence" while
    "tamchi" does not find "tamim". A wrong link here would credit one agency's
    sales to another, so the bar is high and every one is listed on the sheet.
    """
    try:
        from rapidfuzz import fuzz, process
    except ImportError:
        return {}
    choices = [k for k in totals if len(k) >= NEAR_MIN_KEY]
    if not choices:
        return {}
    out: dict = {}
    for key in unmatched:
        if len(key) < NEAR_MIN_KEY:
            continue
        hit = process.extractOne(key, choices, scorer=fuzz.ratio,
                                 score_cutoff=NEAR_SCORE)
        if hit:
            out[key] = hit[0]
    return out


def measure_impact(data: VisitData, sales_rows, *, month: int, year: int,
                   window_days: int = 28) -> ImpactResult:
    """Compare what visited agencies bought before and after, against everyone.

    `sales_rows` is (customer, day, amount). Both sides are measured over the
    SAME two windows -- the days before the month, and the days from its start
    -- because a per-agency window would leave nothing to compare against.
    """
    start = date(year, month, 1)
    pre_from = start - timedelta(days=window_days)
    post_to = start + timedelta(days=window_days - 1)

    totals: dict = defaultdict(lambda: [0.0, 0.0])
    display: dict = {}
    for customer, day, amount in sales_rows:
        key = identity(customer)
        if not key or day is None:
            continue
        amt = float(amount or 0)
        if pre_from <= day < start:
            totals[key][0] += amt
        elif start <= day <= post_to:
            totals[key][1] += amt
        display.setdefault(key, customer)

    first: dict = {}
    visits_of: Counter = Counter()
    rep_of: dict = {}
    name_of: dict = {}
    for v in data.visits:
        key = identity(v.agency)
        if not key:
            continue
        visits_of[key] += 1
        name_of.setdefault(key, v.agency.strip())
        rep_of.setdefault(key, v.rep)
        if v.day and (key not in first or v.day < first[key]):
            first[key] = v.day

    res = ImpactResult(window=f"{pre_from:%d %b} – {start - timedelta(days=1):%d %b} "
                              f"vs {start:%d %b} – {post_to:%d %b %Y}")
    near = _near_matches([k for k in visits_of if k not in totals], totals)
    for key, n in visits_of.items():
        hit = key if key in totals else near.get(key)
        before, after = totals.get(hit, (0.0, 0.0)) if hit else (0.0, 0.0)
        a = AgencyImpact(key=key, name=name_of[key], rep=rep_of[key], visits=n,
                         first_visit=first.get(key), before=before, after=after,
                         matched=hit is not None,
                         match_kind=("exact" if key in totals
                                     else "near" if hit else ""),
                         matched_as=display.get(hit, "") if hit else "")
        res.agencies.append(a)
        if not a.matched:
            continue
        res.visited_before += before
        res.visited_after += after
        if a.first_visit and a.first_visit.day <= EARLY_DAY:
            res.early_before += before
            res.early_after += after
        elif a.first_visit:
            res.late_before += before
            res.late_after += after

    # a like-for-like basis, and a control of comparable size
    sizes = sorted(a.before for a in res.agencies if a.matched and a.before > 0)
    lo = sizes[len(sizes) // 10] if sizes else 0        # ignore the extremes
    hi = sizes[9 * len(sizes) // 10] if sizes else 0
    for a in res.agencies:
        if not a.matched:
            continue
        if a.before > 0:
            res.trading_visited = (res.trading_visited[0] + a.before,
                                   res.trading_visited[1] + a.after)
            if a.match_kind == "exact":
                res.exact_visited = (res.exact_visited[0] + a.before,
                                     res.exact_visited[1] + a.after)
        elif a.after:
            res.started_visited += 1

    claimed = set(visits_of) | set(near.values())
    for key, (before, after) in totals.items():
        if key in claimed:
            continue
        res.control_before += before
        res.control_after += after
        res.control_agencies += 1
        if before > 0:
            res.trading_control = (res.trading_control[0] + before,
                                   res.trading_control[1] + after)
            if lo <= before <= hi:
                res.sized_control = (res.sized_control[0] + before,
                                     res.sized_control[1] + after)
                res.sized_control_n += 1
        elif after:
            res.started_control += 1
    res.unvisited_top = sorted(
        ((display[k], v[1]) for k, v in totals.items() if k not in claimed),
        key=lambda kv: -kv[1])[:40]
    return res


def read_sales_for_impact(source, *, month: int, year: int, window_days: int = 28,
                          progress_cb=None):
    """Ticket sales either side of the reporting month, from the local data."""
    from . import counter_reconcile as cr
    duckdb = cr._duckdb()
    if duckdb is None:
        raise ValueError("duckdb is not available in this build, so visit "
                         "impact cannot be measured.")
    start = date(year, month, 1)
    lo = start - timedelta(days=window_days)
    hi = start + timedelta(days=window_days - 1)
    target = (source.path.as_posix() if source.kind == "gold"
              else source.path.as_posix() + "/**/*.parquet").replace("'", "''")
    if progress_cb is not None:
        progress_cb(0)
    con = duckdb.connect()
    con.execute("SET enable_progress_bar=false")
    rows = con.execute(f"""
        select "Customer", "Pure Date", "Balance (base currency)"
        from read_parquet('{target}')
        where "Transaction" = 'Ticket payment' and "Customer" is not null
          and "Pure Date" between DATE '{lo}' and DATE '{hi}'
    """).fetchall()
    if progress_cb is not None:
        progress_cb(len(rows))
    return rows


def write_impact(ws, res: "ImpactResult", *, month: int, year: int) -> None:
    """Render the impact comparison, with the control always beside the number."""
    from openpyxl.formatting.rule import DataBarRule

    from .counter_master import (BAD, BAND, COLS, GOOD, GREY, LAST, MONEY,
                                 NAVY, PAPER, PCT, WARN, _band, _cell,
                                 _headers, _kpi_strip)

    for col, width in COLS:
        ws.column_dimensions[col].width = width
    ws.sheet_view.showGridLines = False

    ws.merge_cells(f"A1:{LAST}1")
    _cell(ws, 1, 1, "  DID THE VISITS CHANGE ANYTHING?", bold=True, size=16,
          color="FFFFFF", fill=NAVY, align="left")
    ws.row_dimensions[1].height = 34
    ws.merge_cells(f"A2:{LAST}2")
    _cell(ws, 2, 1,
          f"  {res.window}.   Visited agencies are compared with the "
          f"{res.control_agencies:,} agencies nobody visited, over exactly the "
          f"same days — on its own a fall of "
          f"{abs(res.visited_change or 0):.1%} reads as a failure, and it is "
          f"only meaningful next to what happened everywhere else.   This is "
          f"NOT a controlled trial: reps choose whom to visit, so some of the "
          f"difference is who was chosen rather than the visit itself.",
          size=9, color=GREY, fill=PAPER, align="left")
    ws.row_dimensions[2].height = 17

    lift = res.trading_lift or 0
    lo, hi = res.lift_range
    tv, tc = res.trading_visited, res.trading_control
    r = 4
    r = _band(ws, r, "THE ANSWER",
              "both sides restricted to agencies that were already buying "
              "before the month — the like-for-like basis")
    r = _kpi_strip(ws, r, [
        ("Agencies visited", len(res.agencies), "#,##0", None),
        ("Found in the sales data", res.matched, "#,##0", None),
        ("Visited: before", tv[0], MONEY, None),
        ("Visited: after", tv[1], MONEY, None),
        ("Visited change", ImpactResult._rate(*tv), PCT,
         "1F6F3C" if (ImpactResult._rate(*tv) or 0) >= 0 else "C00000"),
        ("Everyone else changed", ImpactResult._rate(*tc), PCT, None),
        ("LIFT (percentage points)", lift * 100, "+0.0;-0.0",
         "1F6F3C" if lift > 0 else "C00000"),
    ])
    r = _kpi_strip(ws, r, [
        (f"Visited by the {EARLY_DAY}th: lift", (res.early_lift or 0) * 100,
         "+0.0;-0.0", "1F6F3C" if (res.early_lift or 0) > 0 else "C00000"),
        ("Visited later: lift", (res.late_lift or 0) * 100, "+0.0;-0.0",
         "1F6F3C" if (res.late_lift or 0) > 0 else "C00000"),
        ("Grew", sum(1 for a in res.agencies if a.verdict == "grew"), "#,##0",
         "1F6F3C"),
        ("Fell", sum(1 for a in res.agencies if a.verdict == "fell"), "#,##0",
         "C00000"),
        ("Started buying",
         sum(1 for a in res.agencies if a.verdict == "started buying"), "#,##0",
         None),
        ("Stopped buying",
         sum(1 for a in res.agencies if a.verdict == "stopped buying"), "#,##0",
         None),
        ("No sales found at all",
         sum(1 for a in res.agencies if not a.matched), "#,##0", "C00000"),
    ])
    r += 1

    # ---- how much does the answer depend on how it was measured? ---------
    r = _band(ws, r, "HOW SURE IS THIS?",
              "the same data measured four defensible ways; the direction is "
              "the same every time, the size is not")
    r = _headers(ws, r, ["Way of measuring it", "Lift (percentage points)",
                         "What it assumes", "", "", "", "", "", "", "", "",
                         "", "", "", "", "", "", "", "", "", ""])
    for label, value, note in (
            ("Already buying, both sides", res.trading_lift,
             "the like-for-like basis, and the one the headline uses"),
            ("Against a control of similar size", res.sized_lift,
             f"only the {res.sized_control_n:,} agencies in the same size band, "
             f"since the visited ones are far larger than the average"),
            ("Exact name matches only", res.exact_lift,
             "ignores every near match, in case one of them links the wrong "
             "agency"),
            ("Against every other agency", res.lift,
             "the widest control, flattered by "
             f"{res.started_control:,} agencies that had no sales before the "
             "month at all and could only go up")):
        _cell(ws, r, 1, label, bold=True, size=10, border=True)
        _cell(ws, r, 2, (value or 0) * 100, fmt="+0.00;-0.00", bold=True,
              size=10, border=True, align="center",
              fill=GOOD if (value or 0) > 0 else BAD)
        ws.merge_cells(start_row=r, start_column=3, end_row=r, end_column=21)
        _cell(ws, r, 3, note, size=9, color=GREY, border=True)
        r += 1
    _cell(ws, r, 1, "Range", bold=True, size=10, border=True, fill=PAPER)
    _cell(ws, r, 2, f"{(lo or 0) * 100:+.2f} to {(hi or 0) * 100:+.2f}",
          bold=True, size=10, border=True, align="center", fill=PAPER)
    ws.merge_cells(start_row=r, start_column=3, end_row=r, end_column=21)
    _cell(ws, r, 3,
          "Positive on every basis. Quote the range, not one number.",
          size=9, color=GREY, border=True, fill=PAPER)
    r += 2

    # ---- per rep ---------------------------------------------------------
    r = _band(ws, r, "BY SALES REP",
              "lift is this rep's change minus what happened to agencies "
              "nobody visited, so a negative month can still be a good one")
    r = _headers(ws, r, [
        "Sales rep", "Agencies visited", "Found in sales", "Before", "After",
        "Change", "Lift vs everyone else", "Grew", "Fell", "Started", "Stopped",
        "No sales found", "", "", "", "", "", "", "", "", "Verdict"])
    per: dict = defaultdict(lambda: Counter())
    for a in res.agencies:
        c = per[a.rep]
        c["n"] += 1
        c[a.verdict] += 1
        if a.matched:
            c["matched"] += 1
            c["before"] += a.before
            c["after"] += a.after
    control = ImpactResult._rate(*res.trading_control) or 0
    first = r
    for rep, c in sorted(per.items(),
                         key=lambda kv: -(kv[1]["after"] - kv[1]["before"])):
        change = ((c["after"] - c["before"]) / c["before"]
                  if c["before"] else None)
        rep_lift = None if change is None else change - control
        if rep_lift is None:
            verdict, tone = "NOTHING TO MEASURE", None
        elif rep_lift > 0.05:
            verdict, tone = "BEAT THE MARKET", GOOD
        elif rep_lift < -0.05:
            verdict, tone = "BEHIND THE MARKET", BAD
        else:
            verdict, tone = "WITH THE MARKET", WARN
        _cell(ws, r, 1, rep, bold=True, size=10, border=True)
        _cell(ws, r, 2, c["n"], size=9, border=True, align="center")
        _cell(ws, r, 3, c["matched"] or None, size=9, border=True,
              align="center")
        _cell(ws, r, 4, c["before"] or None, fmt=MONEY, size=9, border=True,
              align="right")
        _cell(ws, r, 5, c["after"] or None, fmt=MONEY, size=9, border=True,
              align="right")
        _cell(ws, r, 6, change, fmt=PCT, size=9, border=True, align="center")
        _cell(ws, r, 7, rep_lift, fmt="+0.0%;-0.0%", bold=True, size=10,
              border=True, align="center")
        _cell(ws, r, 8, c["grew"] or None, size=9, border=True, align="center")
        _cell(ws, r, 9, c["fell"] or None, size=9, border=True, align="center")
        _cell(ws, r, 10, c["started buying"] or None, size=9, border=True,
              align="center")
        _cell(ws, r, 11, c["stopped buying"] or None, size=9, border=True,
              align="center")
        _cell(ws, r, 12, c["no sales found"] or None, size=9, border=True,
              align="center")
        for j in range(13, 21):
            _cell(ws, r, j, None, border=True)
        _cell(ws, r, 21, verdict, bold=True, size=9, fill=tone, border=True,
              align="center")
        r += 1
    r += 1

    # ---- the agencies that moved ----------------------------------------
    for title, pick, colour in (
            ("AGENCIES THAT GREW MOST AFTER A VISIT",
             lambda xs: sorted((a for a in xs if a.matched),
                               key=lambda a: -(a.after - a.before))[:25], GOOD),
            ("AGENCIES THAT FELL MOST AFTER A VISIT",
             lambda xs: sorted((a for a in xs if a.matched),
                               key=lambda a: (a.after - a.before))[:25], BAD)):
        r = _band(ws, r, title)
        hdr = r
        r = _headers(ws, r, ["Agency", "Rep", "First visit", "Visits", "Before",
                             "After", "Change", "Movement", "", "", "", "", "",
                             "", "", "", "", "", "", "", ""])
        for a in pick(res.agencies):
            _cell(ws, r, 1, a.name, bold=True, size=10, border=True)
            _cell(ws, r, 2, a.rep, size=9, border=True)
            _cell(ws, r, 3, f"{a.first_visit:%d %b}" if a.first_visit else "",
                  size=9, border=True, align="center")
            _cell(ws, r, 4, a.visits, size=9, border=True, align="center")
            _cell(ws, r, 5, a.before or None, fmt=MONEY, size=9, border=True,
                  align="right")
            _cell(ws, r, 6, a.after or None, fmt=MONEY, size=9, border=True,
                  align="right")
            _cell(ws, r, 7, a.change, fmt=PCT, size=9, border=True,
                  align="center")
            _cell(ws, r, 8, a.after - a.before, fmt=MONEY, bold=True, size=10,
                  border=True, align="right", fill=colour)
            for j in range(9, 22):
                _cell(ws, r, j, None, border=True)
            r += 1
        r += 1

    # ---- the two blind spots --------------------------------------------
    r = _band(ws, r, "VISITED, BUT NO SALES FOUND",
              "either they bought nothing, or they buy under another name — "
              "a consolidator's, most likely")
    r = _headers(ws, r, ["Agency", "Rep", "Visits", "First visit", "", "", "",
                         "", "", "", "", "", "", "", "", "", "", "", "", "",
                         ""])
    for a in sorted((x for x in res.agencies if not x.matched),
                    key=lambda x: -x.visits)[:30]:
        _cell(ws, r, 1, a.name, bold=True, size=10, border=True)
        _cell(ws, r, 2, a.rep, size=9, border=True)
        _cell(ws, r, 3, a.visits, size=9, border=True, align="center")
        _cell(ws, r, 4, f"{a.first_visit:%d %b}" if a.first_visit else "",
              size=9, border=True, align="center")
        for j in range(5, 22):
            _cell(ws, r, j, None, border=True)
        r += 1
    r += 1

    r = _band(ws, r, "THE BIGGEST BUYERS NOBODY VISITED",
              "the other half of the question: who is buying without being "
              "called on")
    hdr = r
    r = _headers(ws, r, ["Customer", "Bought in the window", "", "", "", "",
                         "", "", "", "", "", "", "", "", "", "", "", "", "",
                         "", ""])
    for name, amount in res.unvisited_top:
        _cell(ws, r, 1, name, bold=True, size=10, border=True)
        _cell(ws, r, 2, amount, fmt=MONEY, size=10, border=True, align="right")
        for j in range(3, 22):
            _cell(ws, r, j, None, border=True)
        r += 1
    if res.unvisited_top:
        ws.conditional_formatting.add(
            f"B{hdr + 1}:B{r - 1}",
            DataBarRule(start_type="num", start_value=0, end_type="max",
                        color=BAND, showValue=True))

    r += 1
    ws.merge_cells(f"A{r}:{LAST}{r}")
    _cell(ws, r, 1,
          "  How to read this: the lift is the visited agencies' change minus "
          "the change at agencies nobody visited, over identical days. It is "
          "not proof that visiting caused the difference — reps choose whom to "
          "visit, and bigger or friendlier agencies may have held up anyway. "
          "The one internal check available is timing: agencies visited early "
          "in the month, where the visit precedes most of the measured period, "
          "show a larger lift than those visited late. Measured four ways the "
          "answer stays positive but ranges widely, so the range above is the "
          "honest statement of it.",
          size=8, color=GREY, fill=PAPER, wrap=True)
    ws.row_dimensions[r].height = 26
    ws.freeze_panes = "A9"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.orientation = "landscape"


# --------------------------------------------------------------------------
# why a day did not count
# --------------------------------------------------------------------------
#: What each omission means, in the words the sheet prints. The order is the
#: order they are shown: the ones that cost a day first.
OMISSION_REASONS = (
    ("no_date", "No date written",
     "The rep filled the row but left the date blank, so there is no day to "
     "count. The visit is still counted as a visit."),
    ("duplicate", "Duplicate row dropped",
     "The same rep, agency, date and phone already came in from another "
     "file — a Downloads folder routinely holds 'report.xlsx' and "
     "'report (1).xlsx'. Counting both would double the day."),
    ("no_agency", "No agency name",
     "The row had a serial number but no agency, so there was nothing to "
     "record a visit against. It never became a visit."),
    ("blank_row", "Form line never filled in",
     "The printed form carries a serial on every line, so an untouched "
     "line has a number and nothing else. Nothing was written here, so "
     "nothing was lost — it is counted separately from the rows that were."),
    ("date_outside_month", "Date from another month",
     "The rep wrote a date in a different month — usually last month's "
     "template copied forward. The DAY is kept and the month is taken from "
     "this report, so it still counts, but the day it counts on was moved."),
)


@dataclass
class DayAudit:
    """Every input one rep wrote, and whether it reached their Days out."""

    rep: str
    days_out: int = 0
    dated: int = 0                  # visits carrying a usable date
    undated: int = 0                # visits with no date at all
    moved: int = 0                  # date rewritten into this month
    by_reason: dict = field(default_factory=dict)
    details: list = field(default_factory=list)   # (reason label, detail)

    @property
    def blank_rows(self) -> int:
        """Printed form lines nobody filled in. Not lost input."""
        return self.by_reason.get("blank_row", 0)

    @property
    def omitted(self) -> int:
        """Rows somebody WROTE that did not contribute a day.

        Blank form lines are deliberately not here. Counting them made a
        rep who filled 8 of 18 printed lines look like they mislaid 10.
        """
        return (self.undated + self.by_reason.get("duplicate", 0)
                + self.by_reason.get("no_agency", 0))

    @property
    def rows_written(self) -> int:
        """What the rep actually wrote, before anything was dropped."""
        return self.dated + self.omitted

    @property
    def moved_share(self) -> float:
        """How much of their Days out rests on a date taken from elsewhere."""
        return (self.moved / self.dated) if self.dated else 0.0

    @property
    def days_are_moved(self) -> bool:
        """Their Days out is mostly an artefact of a stale template date.

        One rep wrote 39 rows in blocks dated 9 January. Every one was moved
        to 9 September, so 39 rows became ONE day out -- a number that is
        true of the arithmetic and false about the rep.
        """
        return self.moved_share >= 0.5 and self.moved > 1


def day_audit(data: VisitData) -> dict:
    """Per rep: what reached Days out, and why the rest did not.

    Days out counts distinct DATES, so it is not the number of rows a rep
    wrote and was never meant to be. This reconciles the two, so a rep with
    forty rows and nine days can see where the other rows went instead of
    being left to assume the report lost them.
    """
    canon = canonical_people({v.rep for v in data.visits}
                             | {i.rep for i in data.issues})
    out: dict = {}

    def get(rep: str) -> DayAudit:
        name = canon.get(rep, rep)
        got = out.get(name)
        if got is None:
            got = out[name] = DayAudit(rep=name)
        return got

    for v in data.visits:
        a = get(v.rep)
        if v.day:
            a.dated += 1
        else:
            a.undated += 1
        if v.date_moved:
            a.moved += 1
    for rep, audit in out.items():
        audit.days_out = len({v.day for v in data.visits
                              if canon.get(v.rep, v.rep) == rep and v.day})
    labels = dict((k, lab) for k, lab, _why in OMISSION_REASONS)
    for issue in data.issues:
        if issue.kind not in labels:
            continue
        a = get(issue.rep)
        a.by_reason[issue.kind] = a.by_reason.get(issue.kind, 0) + 1
        a.details.append((labels[issue.kind], issue.detail))
    return dict(sorted(out.items(), key=lambda kv: -kv[1].omitted))


def write_day_audit(ws, r: int, data: VisitData) -> int:
    """The 'Days out' reconciliation: every row written, and where it went."""
    from .counter_master import (BAD, GOOD, GREY, LAST, PAPER, WARN, _band,
                                 _cell, _headers)

    audits = day_audit(data)
    r = _band(ws, r, "DAYS OUT — WHY A ROW DID NOT COUNT",
              "most unexplained rows first")
    ws.merge_cells(f"A{r}:{LAST}{r}")
    _cell(ws, r, 1,
          "  'Days out' above counts the distinct DATES a rep filed a usable "
          "visit for — not how many rows they wrote, which it was never meant "
          "to. This reconciles the two, so a rep with forty rows and nine days "
          "can see where the other rows went rather than assume the report "
          "lost them. Every row written is either counted or given a reason.",
          size=9, color=GREY, fill=PAPER, align="left", wrap=True)
    ws.row_dimensions[r].height = 28
    r += 1
    r = _headers(ws, r, [
        "Sales rep", "Days out", "Rows with a date", "No date written",
        "Duplicate dropped", "No agency name", "Date moved into this month",
        "Rows written", "Reached Days out", "Blank form lines", "", "",
        "", "", "", "", "", "", "", "", "Verdict"])
    for a in audits.values():
        dup = a.by_reason.get("duplicate", 0)
        noag = a.by_reason.get("no_agency", 0)
        if a.days_are_moved:
            # the number is true of the arithmetic and false about the rep
            verdict, tone = "DAYS OUT IS A MOVED DATE", BAD
        elif a.undated > max(1, a.rows_written * 0.25):
            verdict, tone = "DATES MISSING", WARN
        elif a.omitted == 0:
            verdict, tone = "ALL ACCOUNTED FOR", GOOD
        else:
            verdict, tone = "SOME ROWS LOST", None
        _cell(ws, r, 1, a.rep, bold=True, size=10, border=True)
        _cell(ws, r, 2, a.days_out or None, bold=True, size=10, border=True,
              align="center")
        _cell(ws, r, 3, a.dated or None, size=9, border=True, align="center")
        _cell(ws, r, 4, a.undated or None, size=9, border=True, align="center",
              fill=BAD if a.undated else None)
        _cell(ws, r, 5, dup or None, size=9, border=True, align="center")
        _cell(ws, r, 6, noag or None, size=9, border=True, align="center")
        # moved rows DID count -- shown so the day they landed on is checkable
        _cell(ws, r, 7, a.moved or None, size=9, border=True, align="center",
              fill=WARN if a.moved else None)
        _cell(ws, r, 8, a.rows_written or None, size=9, border=True,
              align="center")
        _cell(ws, r, 9, a.dated / a.rows_written if a.rows_written else None,
              fmt="0%", size=9, border=True, align="center")
        # shown, but deliberately outside "rows written": nothing was
        # written on a line nobody touched
        _cell(ws, r, 10, a.blank_rows or None, size=9, color=GREY,
              border=True, align="center")
        for j in range(11, 21):
            _cell(ws, r, j, None, border=True)
        _cell(ws, r, 21, verdict, bold=True, size=9, fill=tone, border=True,
              align="center")
        r += 1
    r += 1

    shaky = [a for a in audits.values() if a.days_are_moved]
    if shaky:
        ws.merge_cells(f"A{r}:{LAST}{r}")
        _cell(ws, r, 1,
              "  " + "; ".join(
                  f"{a.rep} wrote {a.dated} dated row(s), {a.moved} of them "
                  f"in blocks dated another month" for a in shaky)
              + ". Those rows all take their day from this report, so they "
                "land together and the Days out beside them counts that one "
                "day. Fix the date in the source file and the figure "
                "corrects itself.",
              size=9, fill=BAD, align="left", wrap=True)
        ws.row_dimensions[r].height = 30
        r += 2

    r = _band(ws, r, "WHAT EACH REASON MEANS")
    for _kind, label, why in OMISSION_REASONS:
        _cell(ws, r, 1, label, bold=True, size=10)
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=21)
        _cell(ws, r, 2, why, size=9, color=GREY, wrap=True)
        ws.row_dimensions[r].height = 26
        r += 1
    r += 1

    r = _band(ws, r, "EVERY OMITTED ROW",
              "so it can be checked against the file, and fixed next month")
    r = _headers(ws, r, ["Sales rep", "Why it did not count", "What was written",
                         "", "", "", "", "", "", "", "", "", "", "", "", "",
                         "", "", "", "", ""])
    listed = 0
    for a in audits.values():
        for label, detail in a.details:
            _cell(ws, r, 1, a.rep, size=9, border=True)
            _cell(ws, r, 2, label, size=9, border=True)
            ws.merge_cells(start_row=r, start_column=3, end_row=r,
                           end_column=21)
            _cell(ws, r, 3, detail or "—", size=9, color=GREY, border=True)
            r += 1
            listed += 1
    if not listed:
        _cell(ws, r, 1,
              "Nothing was omitted: every row written reached a day.",
              size=10, color=GREY)
        r += 1
    return r + 1
