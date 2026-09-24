"""Whether the aircraft we already fly could take one more rotation.

The load workbook says which aircraft TYPE flew each leg and when it left,
but not which tail. So the fleet is rebuilt from the timetable: legs are
chained onto aircraft in time order, an aircraft taking the next departure
from wherever it last landed once it has had its ground time. What falls
out is how many aircraft of each type the day's flying needed.

Two facts follow, and they are all this module claims:

* The busiest day proves the fleet: if eight ATRs flew on one day, eight
  exist. On a day that needed six, two are not flying.
* Between one leg and the next, an aircraft sits on the ground. A window at
  the base long enough for the round trip is a slot the rotation fits in.

What it cannot see: aircraft in heavy maintenance, the standby a schedule
keeps back, crew hours, and the other airport's slot or curfew. "Fits"
means the metal is there -- not that planning has agreed to use it.

Every time here is Dhaka time. The workbook's departure times are local at
the ORIGIN -- DOH-DAC leaves 23:30 on the same date DAC-DOH left 20:10,
which only works in Doha time -- so each is shifted by its station's
offset before anything is chained.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from statistics import median

#: Minutes each station's clock runs BEHIND Dhaka (UTC+6). None of these
#: observe daylight saving.
BEHIND_DHAKA = {
    "CCU": 30, "MAA": 30, "DEL": 30, "BOM": 30, "MLE": 60, "KTM": 15,
    "DXB": 120, "SHJ": 120, "AUH": 120, "FJR": 120, "MCT": 120,
    "DOH": 180, "RUH": 180, "JED": 180, "MED": 180, "DMM": 180,
    "KWI": 180, "BAH": 180, "BKK": -60, "KUL": -120, "SIN": -120,
    "CAN": -120, "HKG": -120,
}

#: Two ground times, for two different jobs.
#:
#: CHAIN_TURN is for the flights ALREADY in the timetable. Those turns are
#: not in question -- the timetable flies them -- so this is only a floor
#: to stop two legs overlapping. The ATRs turn in 30-40 minutes at
#: Chittagong and the outstations (land 07:55, leave 08:25); holding them
#: to 40 minutes would take 13 ATRs to fly the timetable eight fly now.
CHAIN_TURN = 20
#: NEW_TURN is for the EXTRA rotation being tested: before it leaves, at
#: the other end, and back at base before the aircraft's next flight. It is
#: the longest turn operations quote (typically 40 minutes to an hour,
#: 1h15 at most), so a slot that fits here fits on the slowest day.
NEW_TURN = 75

#: A slot only counts if the rotation would leave within operating hours.
#: Jets run red-eyes -- DAC-SIN leaves 22:30 -- so theirs runs to 23:00.
#: The ATRs' last departures are around 20:30; an ATR free at 22:35 is
#: parked for the night, not a Kolkata slot.
EARLIEST_START = 5 * 60
LATEST_START = 23 * 60
LATEST_START_BY = {"ATR 72": 20 * 60 + 30}

DAY = 24 * 60
HUB = "DAC"


#: The 737-800s and the A320 are chained as one pool. The workbook labels
#: the RETURN leg by habit, not by what flew: SHJ-DAC reads "737-800" on
#: all 68 nights, including every one the A320 flew out as DAC-SHJ, and
#: BKK-DAC reads "737-800" with the A320's 180 seats. Chained apart, every
#: such night strands an A320 abroad and invents a 737 -- 135 "737s" and
#: 126 "A320s" in one quarter.
NARROWBODY = "Narrowbody jet (737-800 / A320)"


def family(aircraft) -> str:
    """'ATR-72-600' and 'ATR-72' are the same fleet; so are the spellings
    of the 737. The workbook uses both within one year."""
    key = "".join(ch for ch in str(aircraft or "").lower() if ch.isalnum())
    if "atr" in key:
        return "ATR 72"
    if "737" in key or "320" in key:
        return NARROWBODY
    if "330" in key:
        return "Airbus A330-300"
    if "787" in key:
        return "Boeing 787"
    if "777" in key:
        return "Boeing 777"
    return str(aircraft or "unknown")


def _behind(station: str) -> int:
    return BEHIND_DHAKA.get(station, 0)


def _clock(text):
    """'07:10' -> 430. Anything unreadable -> None."""
    try:
        h, m = str(text).strip().split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def block_times(schedule_rows, airline: str = "BS") -> dict:
    """True gate-to-gate minutes per route, from the market pull.

    The search reports local departure and arrival, so DAC-CCU comes back
    as 30 minutes and CCU-DAC as 90: the hour is real, the clocks differ.
    Each is corrected by the two stations' offsets, and the SHORTEST
    observation is kept, since a longer one is an itinerary with a stop.
    """
    seen = defaultdict(list)
    for row in schedule_rows:
        if row.get("airline") != airline:
            continue
        o, d, m = row.get("origin"), row.get("destination"), row.get("_minutes")
        if not (o and d and m) or o != row.get("queried_origin"):
            continue
        true = m + _behind(d) - _behind(o)
        if true > 0:
            seen[f"{o}-{d}"].append(true)
    return {rt: min(v) for rt, v in seen.items()}


def estimate_block(route: str, km, fam: str):
    """Where the pull never showed the route: from distance and type."""
    if not km:
        return None
    speed, overhead = (7.0, 20) if fam == "ATR 72" else (12.5, 25)
    return round(overhead + km / speed)


@dataclass
class Aircraft:
    ident: int
    station: str
    ready: int                         # Dhaka minutes since the epoch day
    legs: list = field(default_factory=list)   # (dep, arr, route)


@dataclass
class TypeDay:
    """One aircraft type on one day."""
    day: object
    flying: int                        # aircraft that flew at least a leg
    legs: int


@dataclass
class Fleet:
    """The chained timetable, one type at a time."""
    days: dict = field(default_factory=dict)      # family -> [TypeDay]
    aircraft: dict = field(default_factory=dict)  # family -> [Aircraft]
    missing_block: set = field(default_factory=set)
    epoch: object = None

    def proven(self, fam: str) -> int:
        """Aircraft of this type known to exist: the busiest day's count."""
        got = [d.flying for d in self.days.get(fam, [])]
        return max(got) if got else 0

    def typical(self, fam: str):
        got = [d.flying for d in self.days.get(fam, [])]
        return median(got) if got else None

    def flying_on(self, fam: str, day) -> int:
        for d in self.days.get(fam, []):
            if d.day == day:
                return d.flying
        return 0


def _minute(epoch, day, clock, station) -> int:
    return (day - epoch).days * DAY + clock + _behind(station)


def chain(legs, blocks: dict, distances: dict | None = None) -> Fleet:
    """Put every leg on an aircraft, in time order, type by type.

    An aircraft takes the next departure from the station it last landed
    at, once its ground time is up; where none is waiting, the timetable
    needed another aircraft. Of several waiting, the one that landed LAST
    goes, which leaves the longest-waiting one on the ground -- so the
    ground windows this finds are as long as the timetable allows.
    """
    from .route_optimisation import distance_of

    out = Fleet()
    rows = [x for x in legs if _clock(x.std) is not None]
    if not rows:
        return out
    out.epoch = min(x.flight_date for x in rows)
    by_type = defaultdict(list)
    for x in rows:
        fam = family(x.aircraft)
        block = blocks.get(x.leg_route)
        if block is None:
            block = estimate_block(
                x.leg_route, distance_of(x.leg_route, distances or {}), fam)
            if block is None:
                out.missing_block.add(x.leg_route)
                continue
        o, d = x.leg_route.split("-", 1)
        dep = _minute(out.epoch, x.flight_date, _clock(x.std), o)
        by_type[fam].append((dep, dep + block, o, d, x.leg_route,
                             x.flight_date))

    for fam, flights in by_type.items():
        flights.sort()
        ground = CHAIN_TURN
        waiting = defaultdict(list)            # station -> [Aircraft]
        fleet: list = []
        per_day = defaultdict(set)
        legs_per_day = defaultdict(int)
        for dep, arr, o, d, route, day in flights:
            ready = [a for a in waiting[o] if a.ready <= dep]
            if ready:
                plane = max(ready, key=lambda a: a.ready)
                waiting[o].remove(plane)
            else:
                plane = Aircraft(ident=len(fleet) + 1, station=o, ready=dep)
                fleet.append(plane)
            plane.legs.append((dep, arr, route))
            plane.station, plane.ready = d, arr + ground
            waiting[d].append(plane)
            per_day[day].add(plane.ident)
            legs_per_day[day] += 1
        out.aircraft[fam] = fleet
        out.days[fam] = [TypeDay(day, len(per_day[day]), legs_per_day[day])
                         for day in sorted(per_day)]
    return out


#: Candidate departure times are tried this many minutes apart.
STEP = 5


@dataclass
class Slot:
    day: object
    #: Earliest departure (Dhaka clock minutes) that an aircraft already
    #: flying that day could take -- no extra aircraft needed.
    gap: int | None = None
    #: An aircraft sat at the base all day without flying. It may well be
    #: the standby or in maintenance, so it is shown apart, not counted.
    spare: bool = False
    #: How many aircraft are free at once at that time. Pairs showing the
    #: same slot compete for these: one aircraft, one extra rotation.
    aircraft: int = 0

    @property
    def fits(self) -> bool:
        return self.gap is not None or self.spare


class _Ground:
    """How many aircraft of one type stand at one station, minute by minute.

    This does not depend on WHICH aircraft took which leg: every landing
    adds one once its turn is done and every departure takes one away,
    whoever flew it. So the count is a fact of the timetable, where a
    search for gaps on individual aircraft would depend on the chain's
    arbitrary choice of tail -- and did: the same Kolkata slot came and
    went with the chain's ground time.
    """

    def __init__(self, planes, base: str, turn: int = CHAIN_TURN):
        events = []
        for plane in planes:
            if plane.legs and plane.legs[0][2].split("-", 1)[0] == base:
                events.append((-DAY, 0, 1))     # there before it first flew
            for dep, arr, route in plane.legs:
                o, d = route.split("-", 1)
                if o == base:
                    events.append((dep, 1, -1))
                if d == base:
                    events.append((arr + turn, 0, 1))
        events.sort()                           # landings before departures
        self.times, self.counts, n = [], [], 0
        for when, _, delta in events:
            n += delta
            self.times.append(when)
            self.counts.append(n)

    def at(self, when: int) -> int:
        from bisect import bisect_right
        i = bisect_right(self.times, when)
        return self.counts[i - 1] if i else 0

    def lowest(self, start: int, end: int) -> int:
        """The fewest aircraft standing at any moment in [start, end]."""
        from bisect import bisect_right
        low = self.at(start)
        i, j = bisect_right(self.times, start), bisect_right(self.times, end)
        if j > i:
            low = min(low, min(self.counts[i:j]))
        return low


def slots_for(fleet: Fleet, fam: str, base: str, rotation: int) -> list:
    """For each day, the earliest time a round trip of `rotation` minutes
    could leave `base` without an extra aircraft, and how many could.

    The day's SPARE is the fewest aircraft standing at the base at any
    moment of it: at the busiest moment those were not needed at all, so
    they may be the standby or in maintenance, and are shown apart rather
    than counted. The rotation fits when, from departure until it is back
    and turned, the count stays above that spare -- an aircraft that flies
    at other hours is free then -- and the one leaving has had its turn.
    Both counts come from the timetable alone, not from which aircraft
    the chain happened to give which leg.
    """
    planes = fleet.aircraft.get(fam, [])
    days = [d.day for d in fleet.days.get(fam, [])]
    if not planes:
        return []
    ground = _Ground(planes, base)
    # The same count with every landing held back its full 1h15 turn: the
    # aircraft that leaves must be one of these. Where the timetable sent
    # an aircraft out sooner, this count reads low, so it errs towards
    # "no slot", never towards a slot that is not there.
    rested = _Ground(planes, base, turn=NEW_TURN)
    latest = LATEST_START_BY.get(fam, LATEST_START)
    out = []
    for day in days:
        lo = (day - fleet.epoch).days * DAY
        spare = max(0, ground.lowest(lo, lo + DAY - 1))
        slot = Slot(day, spare=spare > 0)
        for clock in range(EARLIEST_START, latest + 1, STEP):
            t = lo + clock
            free = min(ground.lowest(t, t + rotation),
                       rested.at(t)) - spare
            if free >= 1:
                slot.gap, slot.aircraft = clock, free
                break
        out.append(slot)
    return out


def chain_breaks(fleet: Fleet, fam: str) -> int:
    """Aircraft the chain had to invent beyond the proven fleet.

    Each is a departure with no aircraft of the type waiting -- a leg
    missing from the workbook, or a type swap it did not record. Zero
    means the timetable chains cleanly and the slots can be trusted.
    """
    return max(0, len(fleet.aircraft.get(fam, [])) - fleet.proven(fam))


WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass
class RotationCheck:
    """One more rotation on a pair: which weekdays it fits on."""
    pair: str
    base: str
    fam: str
    rotation: int                      # minutes, leaving and back ready
    flown_days: tuple = ()             # weekdays it already operates
    #: weekday -> (days a gap fits, days a spare fits, days seen,
    #:             typical earliest gap start or None,
    #:             typical number of aircraft with a fitting gap)
    by_weekday: dict = field(default_factory=dict)


def _fmt(minutes) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def check_rotation(fleet: Fleet, legs, blocks: dict, outbound: str,
                   distances: dict | None = None):
    """Would one more round trip on `outbound` fit the fleet we fly?

    The type is whatever flies the route now; the length is both blocks
    plus the longest turn at the far end and back at base. None where
    the route's type or
    block times are unknown.
    """
    from .route_optimisation import distance_of

    base, away = outbound.split("-", 1)
    inbound = f"{away}-{base}"
    ours = [x for x in legs if x.leg_route == outbound]
    if not ours:
        return None
    fam = max({family(x.aircraft) for x in ours},
              key=lambda f: sum(1 for x in ours if family(x.aircraft) == f))
    out_b = blocks.get(outbound) or estimate_block(
        outbound, distance_of(outbound, distances or {}), fam)
    in_b = blocks.get(inbound) or estimate_block(
        inbound, distance_of(inbound, distances or {}), fam)
    if not (out_b and in_b):
        return None
    got = RotationCheck(pair=f"{base} ⇄ {away}", base=base, fam=fam,
                        rotation=out_b + NEW_TURN + in_b + NEW_TURN)
    flown = {x.flight_date.strftime("%a") for x in ours}
    got.flown_days = tuple(d for d in WEEKDAYS if d in flown)
    per = defaultdict(list)
    for slot in slots_for(fleet, fam, base, got.rotation):
        per[slot.day.strftime("%a")].append(slot)
    for wd in WEEKDAYS:
        slots = per.get(wd, [])
        gaps = sorted(s.gap for s in slots if s.gap is not None)
        counts = sorted(s.aircraft for s in slots if s.gap is not None)
        got.by_weekday[wd] = (
            len(gaps), sum(1 for s in slots if s.spare), len(slots),
            gaps[len(gaps) // 2] if gaps else None,
            counts[len(counts) // 2] if counts else 0)
    return got
