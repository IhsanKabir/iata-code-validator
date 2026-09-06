"""How often a flight moved, by flight and by route, over a period.

The Flight Change Authenticator reads the same history events one PNR at a
time, to judge whether a reissue was earned. This reads them the other way
round -- one FLIGHT at a time -- to answer a different question: how stable is
this route, how often does this flight move, by how much, how late, and how
many passengers it costs.

Nothing here re-parses Zenith. It consumes `zenith_history_parser.HistoryEvent`
and the change grammar in `flight_change_auth`, both already proven against
real exports.

Three things this module refuses to do, each because doing them produced a
wrong number:

* It never averages a shift across kinds. A TIME_REVISION carries 65 minutes;
  a FLIGHT_TRANSFER carries the gap between DATES -- 47,520 minutes for one
  real 33-day move. Averaged together that reads as a three-thousand-minute
  delay, which is not a fact about anything. Magnitudes are TIME_REVISION only.
* It never presents a de-duplication it did not observe. One schedule change is
  expected to write one event per affected PNR, which is what makes
  "passengers affected" free -- but if no repetition is present in the files,
  that count is a floor and says so.
* It never invents a denominator. "Share of flights changed" needs the roster;
  without it the share is None, not a guess.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from .flight_change_auth import (KIND_CANCEL, KIND_TIME, KIND_TRANSFER,
                                 changes_in_event)

#: Kinds whose shift_minutes is a time-of-day move and may be averaged.
MEASURABLE = (KIND_TIME,)

#: How late the change landed, relative to the departure it moved.
LEAD_BUCKETS = (
    ("more than 7 days", 7 * 24 * 60),
    ("72 hours to 7 days", 72 * 60),
    ("24 to 72 hours", 24 * 60),
    ("under 24 hours", 0),
)


def lead_bucket(minutes: float | None) -> str:
    if minutes is None:
        return "not known"
    if minutes < 0:
        return "after departure"
    for label, floor in LEAD_BUCKETS:
        if minutes >= floor:
            return label
    return "under 24 hours"


@dataclass
class ScheduleChange:
    """One change to one flight, however many PNRs recorded it."""

    flight_number: str
    route: str
    kind: str
    when: datetime | None               # when the change was made
    original_dep: datetime | None
    revised_dep: datetime | None
    shift_minutes: float | None         # signed: + later, - earlier
    detail: str = ""
    changed_by: str = ""
    department: str = ""
    flight_date: str = ""
    pnrs: set = field(default_factory=set)
    rows: int = 0                       # history rows collapsed into this one

    @property
    def passengers(self) -> int:
        """PNRs seen carrying this change. A floor, not a census -- see
        `AggregateResult.repetition_observed`."""
        return len(self.pnrs)

    @property
    def direction(self) -> str:
        if self.kind != KIND_TIME or self.shift_minutes is None:
            return ""
        if self.shift_minutes > 0:
            return "later"
        if self.shift_minutes < 0:
            return "earlier"
        return "no change"

    @property
    def lead_minutes(self) -> float | None:
        """Notice given: change timestamp -> the departure it moved."""
        if self.when is None or self.original_dep is None:
            return None
        return (self.original_dep - self.when).total_seconds() / 60.0

    @property
    def lead(self) -> str:
        return lead_bucket(self.lead_minutes)

    @property
    def is_measurable(self) -> bool:
        return self.kind in MEASURABLE and self.shift_minutes is not None


@dataclass
class FlightRecord:
    """Every change to one flight on one date."""

    flight_number: str
    route: str
    flight_date: str = ""
    changes: list = field(default_factory=list)

    @property
    def n_changes(self) -> int:
        return len(self.changes)

    @property
    def n_time(self) -> int:
        return sum(1 for c in self.changes if c.kind == KIND_TIME)

    @property
    def n_swaps(self) -> int:
        return sum(1 for c in self.changes if c.kind == KIND_TRANSFER)

    @property
    def n_cancelled(self) -> int:
        return sum(1 for c in self.changes if c.kind == KIND_CANCEL)

    @property
    def worst_shift(self) -> float | None:
        """Largest time move, in minutes. Transfers excluded on purpose."""
        got = [abs(c.shift_minutes) for c in self.changes if c.is_measurable]
        return max(got) if got else None

    @property
    def passengers(self) -> int:
        pnrs: set = set()
        for c in self.changes:
            pnrs |= c.pnrs
        return len(pnrs)


@dataclass
class RouteRecord:
    """The rollup one route deserves."""

    route: str
    flights_seen: set = field(default_factory=set)
    flights_scheduled: int | None = None      # from the roster, if loaded
    changes: list = field(default_factory=list)

    @property
    def flights_changed(self) -> int:
        return len(self.flights_seen)

    @property
    def n_changes(self) -> int:
        return len(self.changes)

    @property
    def n_time(self) -> int:
        return sum(1 for c in self.changes if c.kind == KIND_TIME)

    @property
    def n_swaps(self) -> int:
        return sum(1 for c in self.changes if c.kind == KIND_TRANSFER)

    @property
    def n_cancelled(self) -> int:
        return sum(1 for c in self.changes if c.kind == KIND_CANCEL)

    @property
    def _shifts(self) -> list:
        return [abs(c.shift_minutes) for c in self.changes if c.is_measurable]

    @property
    def mean_shift(self) -> float | None:
        got = self._shifts
        return statistics.fmean(got) if got else None

    @property
    def median_shift(self) -> float | None:
        got = self._shifts
        return statistics.median(got) if got else None

    @property
    def worst_shift(self) -> float | None:
        got = self._shifts
        return max(got) if got else None

    @property
    def moved_later(self) -> int:
        return sum(1 for c in self.changes if c.direction == "later")

    @property
    def moved_earlier(self) -> int:
        return sum(1 for c in self.changes if c.direction == "earlier")

    @property
    def passengers(self) -> int:
        pnrs: set = set()
        for c in self.changes:
            pnrs |= c.pnrs
        return len(pnrs)

    @property
    def share_changed(self) -> float | None:
        """Fraction of scheduled flights that moved -- None without a roster.

        Dividing by the flights we happened to SEE changes on would always
        give 100%, which is why this stays None until a roster says otherwise.
        """
        if not self.flights_scheduled:
            return None
        return self.flights_changed / self.flights_scheduled


@dataclass
class AggregateResult:
    changes: list = field(default_factory=list)
    flights: dict = field(default_factory=dict)     # (flight, date) -> FlightRecord
    routes: dict = field(default_factory=dict)      # route -> RouteRecord
    events_read: int = 0
    events_with_change: int = 0
    unrecognised: dict = field(default_factory=dict)   # event_type -> count
    roster_loaded: bool = False

    @property
    def repetition_observed(self) -> bool:
        """Did any single change actually arrive on more than one row?

        This is the assumption the passenger count rests on. When it is false,
        every change was seen once and `passengers` is a floor rather than a
        measurement -- which the report must say rather than imply otherwise.
        """
        return any(c.rows > 1 for c in self.changes)

    @property
    def n_time(self) -> int:
        return sum(1 for c in self.changes if c.kind == KIND_TIME)

    @property
    def n_swaps(self) -> int:
        return sum(1 for c in self.changes if c.kind == KIND_TRANSFER)

    @property
    def n_cancelled(self) -> int:
        return sum(1 for c in self.changes if c.kind == KIND_CANCEL)

    @property
    def passengers(self) -> int:
        pnrs: set = set()
        for c in self.changes:
            pnrs |= c.pnrs
        return len(pnrs)


def _route_of(event, change) -> str:
    """Which route this change belongs to.

    A transfer carries no route of its own -- verified: FLIGHT_TRANSFER comes
    back with route=''. Reading the change alone would file every swap under
    'unknown'. The event's own flight cell is the better witness; the change's
    route is the fallback.
    """
    flight = getattr(event, "flight", None)
    orig = (getattr(flight, "origin", "") or "").strip()
    dest = (getattr(flight, "destination", "") or "").strip()
    if orig and dest:
        return f"{orig}-{dest}"
    raw = (getattr(change, "route", "") or "").strip().upper()
    if len(raw) == 6 and raw.isalpha():
        return f"{raw[:3]}-{raw[3:]}"
    return raw or "(route not written)"


def _key(flight_number, route, change, when):
    """What makes two rows the same change.

    Timestamps in these exports are minute-granular -- every one observed had
    zero seconds -- so the minute is the native resolution and rounding adds
    nothing and risks nothing.
    """
    return (flight_number, route, change.kind, when,
            change.original_dep, change.revised_dep)


def aggregate(events, roster=None) -> AggregateResult:
    """Collapse per-PNR history rows into per-flight schedule changes.

    `roster` is an optional iterable of `zenith_history_downloader.FlightRef`
    -- the scheduled flights for the period. It supplies the denominator for
    'share of flights changed'; without it that share stays None.
    """
    res = AggregateResult()
    grouped: dict = {}

    # Pass one: pull out every change with whatever identity its row carried.
    # A history row can name the change and leave the flight cell empty -- two
    # of five in the real sample did -- so the flight is recovered afterwards
    # rather than filed under "not written", which split one flight's history
    # across two buckets and counted it as two flights.
    staged: list = []
    for event in events:
        res.events_read += 1
        found = changes_in_event(event)
        if not found:
            etype = (getattr(event, "event_type", "") or "").strip()
            if etype:
                res.unrecognised[etype] = res.unrecognised.get(etype, 0) + 1
            continue
        res.events_with_change += 1

        flight = getattr(event, "flight", None)
        number = (getattr(flight, "flight_number", "") or "").strip()
        fdate = (getattr(flight, "flight_date", "") or "").strip()
        when = getattr(event, "timestamp", None)
        agent = getattr(event, "agent", None)
        pnr = (getattr(event, "pnr", "") or "").strip()

        for change in found:
            route = _route_of(event, change)
            # The departure being moved dates the flight even when the cell is
            # blank; it is the same fact written elsewhere on the row.
            if not fdate and change.original_dep is not None:
                fdate = change.original_dep.strftime("%d/%m/%Y")
            staged.append((event, change, route, number, fdate, when, agent, pnr))

    # Pass two: a blank flight number is adopted ONLY from a number actually
    # observed on the same route and date. Never guessed, never invented.
    known: dict = {}
    for _e, _c, route, number, fdate, *_rest in staged:
        if number and fdate:
            known.setdefault((route, fdate), number)

    for event, change, route, number, fdate, when, agent, pnr in staged:
        if not number:
            number = known.get((route, fdate), "")
        # A flight with no number anywhere is still a flight: the route and the
        # date it departed identify it well enough to count.
        label = number or route or "(flight not written)"
        k = _key(label, route, change, when)
        slot = grouped.get(k)
        if slot is None:
            slot = grouped[k] = ScheduleChange(
                flight_number=label,
                route=route, kind=change.kind, when=when,
                original_dep=change.original_dep,
                revised_dep=change.revised_dep,
                shift_minutes=change.shift_minutes,
                detail=change.detail,
                changed_by=(getattr(agent, "display_name", "") or ""),
                department=(getattr(agent, "department", "") or ""),
                flight_date=fdate,
            )
        slot.rows += 1
        if pnr:
            slot.pnrs.add(pnr)

    # deterministic order: when it happened, then the flight it happened to
    res.changes = sorted(
        grouped.values(),
        key=lambda c: (c.when or datetime.min, c.flight_number, c.route,
                       c.kind))

    for change in res.changes:
        fkey = (change.flight_number, change.flight_date)
        rec = res.flights.get(fkey)
        if rec is None:
            rec = res.flights[fkey] = FlightRecord(
                flight_number=change.flight_number, route=change.route,
                flight_date=change.flight_date)
        rec.changes.append(change)

        rt = res.routes.get(change.route)
        if rt is None:
            rt = res.routes[change.route] = RouteRecord(route=change.route)
        rt.changes.append(change)
        rt.flights_seen.add(fkey)

    if roster is not None:
        res.roster_loaded = True
        scheduled: dict = defaultdict(int)
        for f in roster:
            orig = (getattr(f, "origin", "") or "").strip()
            dest = (getattr(f, "destination", "") or "").strip()
            if orig and dest:
                scheduled[f"{orig}-{dest}"] += 1
        for route, rec in res.routes.items():
            rec.flights_scheduled = scheduled.get(route) or None

    return res


def detect_period(events):
    """The month these history rows are about, by majority vote of the dates.

    Asking for a month is asking to be given the wrong one: a run defaulted to
    today's month once already and relabelled a whole month of another one.
    The rows carry the flight dates, so the period is read rather than typed.

    Returns (month, year, votes, agreement) -- or (None, None, {}, 0.0) when
    nothing readable is present.
    """
    votes: Counter = Counter()
    for event in events:
        flight = getattr(event, "flight", None)
        raw = (getattr(flight, "flight_date", "") or "").strip()
        if raw:
            parts = raw.split("/")
            if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                votes[(int(parts[1]), int(parts[2]))] += 1
                continue
        for change in changes_in_event(event):
            if change.original_dep is not None:
                votes[(change.original_dep.month, change.original_dep.year)] += 1
                break
    if not votes:
        return None, None, {}, 0.0
    (month, year), hits = votes.most_common(1)[0]
    return month, year, dict(votes), hits / sum(votes.values())
