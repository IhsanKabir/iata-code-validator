"""The counter day-sheet blocks beyond the sales ones: enquiries and time.

These were deliberately left out of the master sheet while their reliability was
unknown. They are in now, on one condition: what the staff did NOT write is
counted and printed next to what they did. A conversion rate over a denominator
nobody filled is worse than no conversion rate, so every figure here carries the
number of rows it could not see.

Nothing in this module infers a missing value. A blank stays blank and is
reported as "not written".
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

NOT_WRITTEN = "not written"

# A row of six hours or more is the whole shift, not a task. Some counters log
# one "09 HOURS" line for the day while others log each job in minutes; adding
# the two together produced 531 hours from 61 rows. They are counted apart.
SHIFT_MINUTES = 360

_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(MINUTES?|MINS?|HOURS?|HRS?)\b", re.I)
_BLOCK_TITLE = re.compile(r"others?\s+work", re.I)
_UNKNOWN_TIME = re.compile(r"unable|n/?a|not\s*count", re.I)

# what the convert column says, and what it means
_CONVERTED = ("YES", "BOOKED", "SOLD", "DONE", "ISSUED", "CONFIRM")
_LOST = ("NO", "N0", "FARE HIGH", "HIGH PRICE", "SOLD OUT", "NOT ")
_PENDING = ("PENDING", "BOOKING", "HOLD", "WAIT")


@dataclass
class QueryRow:
    counter: str
    day: int | None
    method: str = ""          # Walk-in / Phone Call / WhatsApp
    qtype: str = ""           # what was asked about
    segment: str = ""         # the route enquired about
    outcome: str = ""         # CONVERTED / LOST / PENDING / NOT WRITTEN
    reason: str = ""          # the raw text, when it carries a reason
    received_by: str = ""     # almost never filled -- that is the point


@dataclass
class TimeRow:
    counter: str
    day: int | None
    emp_id: str = ""
    shift: str = ""
    task: str = ""
    minutes: float | None = None      # None means the staff left it blank


@dataclass
class ActivitySummary:
    """Counts first, and always alongside what could not be counted."""
    queries: list = field(default_factory=list)
    times: list = field(default_factory=list)

    # ---- enquiries ----
    @property
    def query_total(self) -> int:
        return len(self.queries)

    @property
    def outcomes(self) -> Counter:
        return Counter(q.outcome for q in self.queries)

    @property
    def judged(self) -> int:
        """Rows whose outcome the staff actually recorded."""
        return sum(v for k, v in self.outcomes.items() if k != NOT_WRITTEN)

    @property
    def converted(self) -> int:
        return self.outcomes.get("converted", 0)

    @property
    def conversion(self) -> float | None:
        """Only over rows with a recorded outcome, or None if there are none."""
        base = self.converted + self.outcomes.get("lost", 0)
        return self.converted / base if base else None

    @property
    def attributed(self) -> int:
        return sum(1 for q in self.queries if q.received_by)

    # ---- time ----
    @property
    def time_rows(self) -> int:
        return len(self.times)

    @property
    def timed_rows(self) -> int:
        return sum(1 for t in self.times if t.minutes is not None)

    @property
    def minutes(self) -> float:
        """Task minutes only -- whole-shift rows are counted separately."""
        return sum(t.minutes for t in self.times
                   if t.minutes is not None and t.minutes < SHIFT_MINUTES)

    @property
    def shift_rows(self) -> int:
        return sum(1 for t in self.times
                   if t.minutes is not None and t.minutes >= SHIFT_MINUTES)

    def minutes_by_employee(self) -> dict:
        out: dict = defaultdict(lambda: {"minutes": 0.0, "rows": 0, "blank": 0})
        for t in self.times:
            if not t.emp_id:
                continue
            slot = out[t.emp_id]
            slot["rows"] += 1
            if t.minutes is None:
                slot["blank"] += 1
            else:
                slot["minutes"] += t.minutes
        return dict(out)

    def by_counter(self) -> dict:
        out: dict = defaultdict(lambda: Counter())
        for q in self.queries:
            c = out[q.counter]
            c["queries"] += 1
            c[q.outcome] += 1
            if q.received_by:
                c["attributed"] += 1
        for t in self.times:
            c = out[t.counter]
            c["time_rows"] += 1
            if t.minutes is None:
                c["time_blank"] += 1
            elif t.minutes >= SHIFT_MINUTES:
                c["shift_rows"] += 1
            else:
                c["minutes"] += t.minutes
        return {k: dict(v) for k, v in out.items()}


def classify_outcome(raw: str) -> tuple[str, str]:
    """('converted'|'lost'|'pending'|NOT_WRITTEN, the reason text).

    The column is free text: YES, NO, N0, PENDING, 'NO(HIGH PRICE)',
    'NO (NO SEATS ARE AVAILABLE)', SOLD OUT. The reason is worth keeping -- it
    separates losing a sale on price from losing it on availability.
    """
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not text:
        return NOT_WRITTEN, ""
    up = text.upper()
    reason = text if len(text) > 4 else ""
    if up.startswith(_PENDING):
        return "pending", reason
    if up.startswith(_LOST):
        return "lost", reason
    if up.startswith(_CONVERTED):
        return "converted", reason
    return NOT_WRITTEN, text


def parse_duration(cells) -> float | None:
    """Minutes, or None when the staff wrote nothing usable.

    'UNABLE TO COUNT' is not zero and must never be averaged as zero; it is a
    row the counter chose not to time, and it is reported as such.
    """
    text = " ".join(str(c).strip() for c in cells if c is not None)
    if _BLOCK_TITLE.search(text) or not text:
        return None
    m = _DURATION.search(text)
    if not m:
        return None
    if _UNKNOWN_TIME.search(text) and not m:
        return None
    value = float(m.group(1))
    return value * (60 if m.group(2).upper().startswith("H") else 1)


# --------------------------------------------------------------------------
# parsing the blocks out of a day sheet
# --------------------------------------------------------------------------
def _hkey(v) -> str:
    return re.sub(r"\s+", " ", str(v)).strip().lower().rstrip("?:").strip() \
        if v is not None else ""


def _txt(v) -> str:
    return re.sub(r"\s+", " ", str(v)).strip() if v is not None else ""


_QUERY_BREAK = ("ffp", "received mail", "shift name", "sky star", "mail service",
                "others work", "total count", "total ")
_TIME_BREAK = ("ffp", "received mail", "query method", "sky star", "mail service")


def parse_activity(grid, counter: str, day: int | None, emp_id_re) -> ActivitySummary:
    """Pull the enquiry log and the timesheet out of one day sheet."""
    out = ActivitySummary()
    i = 0
    while i < len(grid):
        keys = [_hkey(c) for c in grid[i]]

        if "query method" in keys:
            cmap = {k: j for j, k in enumerate(keys) if k}
            method = ""
            i += 1
            while i < len(grid):
                row = grid[i]
                rk = [_hkey(c) for c in row]
                if any(k.startswith(_QUERY_BREAK) for k in rk) or "action" in rk:
                    break

                def g(name, row=row):
                    j = cmap.get(name)
                    return _txt(row[j]) if j is not None and j < len(row) else ""

                if g("query method"):
                    method = g("query method")
                qtype = g("query type")
                raw = g("did query convert to sale")
                if qtype or raw:
                    outcome, reason = classify_outcome(raw)
                    out.queries.append(QueryRow(
                        counter=counter, day=day, method=method.upper(),
                        qtype=qtype.upper(), segment=g("segment").upper(),
                        outcome=outcome, reason=reason,
                        received_by=g("received by")))
                i += 1
            continue

        if any(k.startswith("shift name") for k in keys):
            cmap = {k: j for j, k in enumerate(keys) if k}
            i += 1
            while i < len(grid):
                row = grid[i]
                rk = [_hkey(c) for c in row]
                if any(k.startswith(_TIME_BREAK) for k in rk) or "action" in rk:
                    break

                def g2(name, row=row):
                    j = cmap.get(name)
                    return _txt(row[j]) if j is not None and j < len(row) else ""

                eid = next((_txt(c).upper().replace(" ", "-") for c in row
                            if emp_id_re.match(_txt(c))), "")
                if eid:
                    task = ""
                    for key in ("file management", "nature of query by customer",
                                "others query"):
                        for kk, j in cmap.items():
                            if kk.startswith(key) and j < len(row) and _txt(row[j]):
                                task = _txt(row[j]).upper()
                                break
                        if task:
                            break
                    out.times.append(TimeRow(
                        counter=counter, day=day, emp_id=eid,
                        shift=g2("shift name").upper(), task=task,
                        minutes=parse_duration(row)))
                i += 1
            continue
        i += 1
    return out


def merge(summaries) -> ActivitySummary:
    total = ActivitySummary()
    for s in summaries:
        total.queries.extend(s.queries)
        total.times.extend(s.times)
    return total


# --------------------------------------------------------------------------
# naming the staff behind an unreported PNR
# --------------------------------------------------------------------------
def enrich_from_zenith(session, findings, *, lookup, max_lookups: int = 300,
                       stop_flag=None, progress_cb=None) -> dict:
    """Look unreported PNRs up live and record who handled them.

    The sales data names a Sale agent, but not everything about who touched the
    booking. For a PNR the counter never wrote down, that is the only way left to
    put a name to it -- so the lookup is offered rather than the question left
    open. Failures are recorded as such; a PNR that cannot be read is reported
    as unknown, never as nobody.
    """
    seen: dict[str, dict] = {}
    todo = []
    for f in findings:
        code = (getattr(f, "locator", "") or "").strip().upper()
        if code and code not in seen:
            seen[code] = {}
            todo.append(code)
    todo = todo[:max_lookups]
    done = 0
    for code in todo:
        if stop_flag is not None and stop_flag():
            break
        done += 1
        if progress_cb is not None:
            progress_cb(done, len(todo), code)
        try:
            d = lookup(session, code)
        except Exception as exc:                      # noqa: BLE001
            seen[code] = {"status": f"lookup failed: {type(exc).__name__}"}
            continue
        if d is None:
            seen[code] = {"status": "not found"}
            continue
        seen[code] = {
            "customer": getattr(d, "customer_name", "") or "",
            "phone": getattr(d, "phone", "") or "",
            "status": getattr(d, "pnr_status", "") or "",
            "pax": getattr(d, "pax_count", "") or "",
            "route": getattr(d, "booked_route", "") or "",
        }
    for f in findings:
        code = (getattr(f, "locator", "") or "").strip().upper()
        info = seen.get(code)
        if not info:
            continue
        if not getattr(f, "customer", ""):
            f.customer = info.get("customer", "")
        extra = " · ".join(v for v in (info.get("status", ""),
                                       info.get("route", "")) if v)
        if extra:
            f.note = (f.note + " · " if f.note else "") + extra
    return seen
