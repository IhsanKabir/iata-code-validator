"""Whether the current flying is the right flying, route by route.

Four sources answer one question. Our own load workbook says how full each
flight goes and what aircraft flew it. A live schedule pull says who else
flies the route and on what. The distance table turns a big route and a small
one into comparable numbers. And the revenue file says what the seats earn.

The thing that makes the answer move is SEATS rather than flights. On
DAC-CCU an IndiGo A320 against our ATR is 180 seats to 72, so counting
departures says we hold a third of that market and counting seats says a
tenth -- and only the second explains why we fly at 92% and still lose share.

Four rules, each because the obvious alternative produced a wrong list:

* A connecting itinerary is not a competitor on the leg. The feed returns a
  Singapore 787 for DAC-CCU routed through Changi, 1,683 minutes gate to
  gate. Anything far longer than the sector's own block time is held back.
* A fare is not a flight. One departure comes back once per fare variant, so
  frequency counts distinct (airline, flight number, departure).
* Absence is not zero. What a search shows is what is ON SALE, so a sold-out
  flight can vanish; every frequency here is a floor, and the days actually
  observed are carried beside it.
* A route with two observed flights is not a market share. Anything under a
  usable sample is reported as thin rather than ranked against the rest.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Longest a nonstop leg can plausibly take, as a multiple of the shortest
#: block time seen on that route. A one-stop through a hub is several times
#: the nonstop; a nonstop varies by maybe a quarter.
NONSTOP_FACTOR = 2.0
#: And an absolute ceiling, for routes where every observation is a one-stop.
NONSTOP_CEILING_MINUTES = 240

#: Below this many observed departures, a share is noise rather than a fact.
THIN_FLIGHTS = 6

#: A route we fill above this is a candidate for more capacity.
FULL_ENOUGH = 0.90


@dataclass
class Rival:
    airline: str
    flights: float = 0.0          # per day
    seats: float = 0.0            # per day
    aircraft: str = ""
    seat_basis: str = ""


@dataclass
class RouteView:
    """One directed route: how we fly it, and who else does."""

    route: str
    our_flights: float = 0.0      # per day, from our own records
    our_seats: float = 0.0        # per day
    our_taken: float = 0.0        # seats sold or flown, per day
    our_legs: int = 0
    aircraft: str = ""
    rivals: list = field(default_factory=list)
    distance_km: float | None = None
    revenue_month: float | None = None
    passengers_month: float | None = None
    observed_days: int = 0

    @property
    def load_factor(self):
        return (self.our_taken / self.our_seats) if self.our_seats else None

    @property
    def rival_seats(self) -> float:
        return sum(r.seats for r in self.rivals)

    @property
    def rival_flights(self) -> float:
        return sum(r.flights for r in self.rivals)

    @property
    def market_seats(self) -> float:
        return self.our_seats + self.rival_seats

    @property
    def seat_share(self):
        return (self.our_seats / self.market_seats) if self.market_seats \
            else None

    @property
    def flight_share(self):
        total = self.our_flights + self.rival_flights
        return (self.our_flights / total) if total else None

    @property
    def top_rival(self):
        return max(self.rivals, key=lambda r: r.seats) if self.rivals else None

    @property
    def thin(self) -> bool:
        """Too few observations for the share to mean anything."""
        return self.our_legs < THIN_FLIGHTS or not self.rivals

    @property
    def rask(self):
        """Revenue per available seat-kilometre, the comparable measure.

        A 95% load factor on a 329 km hop and on a 4,000 km sector are not
        the same opportunity, and only distance makes them comparable.
        """
        if not (self.revenue_month and self.distance_km and self.our_seats):
            return None
        ask = self.our_seats * 30.0 * self.distance_km
        return self.revenue_month / ask if ask else None

    @property
    def squeezed(self) -> bool:
        """Full aircraft, small share of the seats: the shape worth acting on."""
        lf, share = self.load_factor, self.seat_share
        return bool(lf and share and lf >= FULL_ENOUGH and share < 0.35
                    and not self.thin)

    def verdict(self) -> str:
        if self.thin:
            return "too few flights to judge"
        lf, share = self.load_factor, self.seat_share
        if lf is None or share is None:
            return "not comparable"
        if self.squeezed:
            return "FULL AND OUT-FLOWN — candidate for more capacity"
        if lf >= FULL_ENOUGH:
            return "full, and already holds the market"
        if lf < 0.65:
            return "capacity ahead of demand"
        return "steady"


@dataclass
class Result:
    routes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    unknown_aircraft: dict = field(default_factory=dict)
    window: tuple = ()
    load_span: tuple = ()
    basis: str = ""

    @property
    def squeezed(self) -> list:
        got = [r for r in self.routes if r.squeezed]
        got.sort(key=lambda r: (r.seat_share or 1.0))
        return got

    @property
    def comparable(self) -> list:
        return [r for r in self.routes if not r.thin]

    def by_route(self, route: str):
        want = route.upper()
        for r in self.routes:
            if r.route.upper() == want:
                return r
        return None

    def summary(self) -> str:
        return (f"{len(self.routes)} route(s), {len(self.comparable)} with "
                f"enough flights to compare, {len(self.squeezed)} full and "
                f"out-flown")


def load_distances(path) -> dict:
    """Route -> kilometres, from the distance workbook.

    The first sheet labels its columns KM then NM while holding NM then KM,
    and leaves DAC-JSR blank where the later sheet has it -- so the LAST
    sheet is read, and the larger of the two numbers is taken as the
    kilometres, since a kilometre figure is always the bigger one.
    """
    from openpyxl import load_workbook

    out: dict = {}
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                if not row or len(row) < 4:
                    continue
                a = str(row[0] or "").strip().upper()
                b = str(row[1] or "").strip().upper()
                if len(a) != 3 or len(b) != 3 or not a.isalpha():
                    continue
                nums = []
                for cell in row[2:4]:
                    try:
                        nums.append(float(str(cell).replace(",", "")))
                    except (TypeError, ValueError):
                        pass
                if nums:
                    km = max(nums)
                    if km > 0:
                        out[f"{a}-{b}"] = km
    finally:
        wb.close()
    return out


def _nonstop_cutoff(minutes) -> float:
    """How long a nonstop on this route may take, from the route itself."""
    real = [m for m in minutes if m and m > 0]
    if not real:
        return NONSTOP_CEILING_MINUTES
    return min(min(real) * NONSTOP_FACTOR, NONSTOP_CEILING_MINUTES)


def build(load_legs, schedule_rows, *, distances=None, revenue=None,
          days_observed: int = 14, our_airline: str = "BS") -> Result:
    """Fold our loads and the market schedule into one view per route.

    `schedule_rows` are mappings with queried_origin, queried_destination,
    airline, flight_number, departure, arrival, aircraft and queried_date.
    """
    from collections import defaultdict

    from . import aircraft_seats as seats

    res = Result(basis="", window=(), load_span=())
    own_table = seats.operator_table(load_legs)

    ours = defaultdict(lambda: {"legs": 0, "seats": 0.0, "taken": 0.0,
                                "ac": ""})
    bases = set()
    for leg in load_legs:
        if leg.load_factor is None:
            continue
        got = ours[leg.leg_route]
        got["legs"] += 1
        got["seats"] += leg.capacity
        got["taken"] += leg.seats_taken
        got["ac"] = got["ac"] or leg.aircraft
        bases.add(leg.basis)
    res.basis = "/".join(sorted(b for b in bases if b))
    if load_legs:
        days = [x.flight_date for x in load_legs]
        res.load_span = (min(days), max(days))

    # group the schedule by route, then decide the nonstop cutoff per route
    by_route = defaultdict(list)
    dates = set()
    for row in schedule_rows:
        rt = f"{row.get('queried_origin')}-{row.get('queried_destination')}"
        by_route[rt].append(row)
        if row.get("queried_date"):
            dates.add(str(row["queried_date"]))
    if dates:
        res.window = (min(dates), max(dates))

    span = max(1, (res.load_span[1] - res.load_span[0]).days + 1) \
        if res.load_span else 1

    for rt, rows in by_route.items():
        mins = [r.get("_minutes") for r in rows]
        cutoff = _nonstop_cutoff(mins)
        per_airline = defaultdict(lambda: {"f": set(), "ac": "", "seats": 0.0})
        for row in rows:
            m = row.get("_minutes")
            if m is None or m > cutoff:
                continue            # a one-stop through a hub, not this leg
            if row.get("origin") != row.get("queried_origin"):
                continue
            al = str(row.get("airline") or "")
            got = per_airline[al]
            got["f"].add((row.get("flight_number"), str(row.get("departure"))))
            got["ac"] = got["ac"] or str(row.get("aircraft") or "")

        mine = ours.get(rt, {})
        view = RouteView(
            route=rt,
            our_legs=mine.get("legs", 0),
            our_flights=mine.get("legs", 0) / span,
            our_seats=mine.get("seats", 0.0) / span,
            our_taken=mine.get("taken", 0.0) / span,
            aircraft=mine.get("ac", ""),
            observed_days=days_observed,
            distance_km=(distances or {}).get(rt),
        )
        rev = (revenue or {}).get(rt)
        if rev:
            view.revenue_month, view.passengers_month = rev
        for al, got in per_airline.items():
            if al == our_airline:
                continue
            est = seats.seats_for(got["ac"], al, None)
            per_day = len(got["f"]) / max(days_observed, 1)
            if not est.known:
                res.unknown_aircraft[(al, got["ac"])] = len(got["f"])
                continue
            view.rivals.append(Rival(
                airline=al, flights=per_day, seats=per_day * est.seats,
                aircraft=got["ac"], seat_basis=est.confidence))
        view.rivals.sort(key=lambda r: -r.seats)
        res.routes.append(view)

    res.routes.sort(key=lambda r: (r.seat_share if r.seat_share is not None
                                   else 1.0))
    if res.unknown_aircraft:
        res.warnings.append(
            f"{sum(res.unknown_aircraft.values())} competitor flight(s) carry "
            f"an aircraft with no known seat count and are left out of the "
            f"share rather than given a guessed one.")
    res.warnings.append(
        "Competitor frequency is what is ON SALE, so it is a floor: a "
        "sold-out flight can be invisible. Our own frequency comes from our "
        "records, not from the search.")
    if res.basis and "sold" in res.basis:
        res.warnings.append(
            "Load factor uses seats SOLD where the workbook no longer carries "
            "flown, which is before no-shows and reads slightly high.")
    return res
