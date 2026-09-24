"""Base fare per leg per month, out of the sales warehouse.

The warehouse stores one row per TICKET, not per leg: the whole ticket's
base fare sits on a single row labelled with the ticket's FIRST leg, and
`seg` counts the legs rather than numbering them. A DAC-CGP-DAC return is
one row -- DAC to CGP, 14,349 base -- with nothing at all against CGP-DAC.

Summing base fare by departure and arrival airport therefore gets two
things badly wrong, and this module exists to get them right:

* A return credits its whole fare to the outbound leg and nothing to the
  inbound one. 5,040 DAC-CGP-DAC tickets in July and August carried 47.95M
  that way.
* A connecting ticket credits a long-haul fare to its first, short leg.
  CGP-DAC-DXB carried 31.11M over the same months, most of it Dubai, and
  all of it would have landed on a 290 km domestic hop.

So each ticket's base fare is split across the legs in its routing,
weighted by distance -- the mileage prorate airlines use between
themselves. Where the table lacks a leg, its great-circle length from
airport positions weights the split instead; only where even that is
unknown is the ticket split equally. Both are counted, so the report can
say how much rests on the cruder splits. Refunds and voids carry negative base fare and go through the same
split, so the result is base fare net of both.

Every leg is dated to the ticket's first flight: the warehouse holds no
later date. A return whose second leg falls in the next month is therefore
filed a month early, which matters only at month edges.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

#: Transaction lines whose base fare is revenue. Refunds and voids are
#: already negative in the warehouse, so netting is a plain sum.
REVENUE_LINES = ("Ticket payment", "Refund", "Ticket void")


def legs_of(routing) -> list:
    """'DAC-CGP-DAC' -> ['DAC-CGP', 'CGP-DAC']. Anything unreadable -> []."""
    points = [p.strip().upper() for p in str(routing or "").split("-")]
    points = [p for p in points if p]
    if len(points) < 2 or not all(len(p) == 3 and p.isalpha()
                                  for p in points):
        return []
    return [f"{a}-{b}" for a, b in zip(points, points[1:]) if a != b]


#: Airport positions (lat, lon), used ONLY to weight the fare split for a
#: leg the distance table lacks. The table has no CGP-MCT, CGP-DXB or
#: CGP-DOH, and an equal split handed half of a DXB-CGP-DAC fare to the
#: 300 km domestic leg: 9.8M too much on CGP-DAC over July and August. A
#: great-circle length puts the fare where the flying is. These never
#: appear in the report as a route's distance -- that stays blank until
#: the table carries it.
AIRPORTS = {
    "DAC": (23.843, 90.398), "CGP": (22.250, 91.813),
    "ZYL": (24.963, 91.867), "CXB": (21.452, 91.964),
    "JSR": (23.184, 89.161), "RJH": (24.437, 88.617),
    "SPD": (25.759, 88.909), "BZL": (22.801, 90.301),
    "CCU": (22.654, 88.447), "MAA": (12.990, 80.169),
    "DEL": (28.556, 77.100), "BOM": (19.089, 72.868),
    "KTM": (27.697, 85.359), "CMB": (7.180, 79.884),
    "MLE": (4.192, 73.529), "DXB": (25.253, 55.366),
    "SHJ": (25.329, 55.517), "AUH": (24.433, 54.651),
    "FJR": (25.112, 56.324), "DOH": (25.273, 51.608),
    "MCT": (23.593, 58.284), "BAH": (26.271, 50.634),
    "KWI": (29.227, 47.969), "DMM": (26.471, 49.798),
    "RUH": (24.958, 46.699), "JED": (21.680, 39.157),
    "MED": (24.553, 39.705), "KUL": (2.746, 101.710),
    "SIN": (1.364, 103.991), "BKK": (13.690, 100.750),
    "CAN": (23.392, 113.299), "HKG": (22.308, 113.918),
    "NRT": (35.772, 140.393), "LHR": (51.470, -0.454),
    "MAN": (53.354, -2.275), "YYZ": (43.677, -79.625),
}


def great_circle_km(a: str, b: str):
    """Shortest distance over the earth between two known airports."""
    from math import asin, cos, radians, sin, sqrt

    if a not in AIRPORTS or b not in AIRPORTS:
        return None
    (la1, lo1), (la2, lo2) = AIRPORTS[a], AIRPORTS[b]
    h = (sin(radians(la2 - la1) / 2) ** 2
         + cos(radians(la1)) * cos(radians(la2))
         * sin(radians(lo2 - lo1) / 2) ** 2)
    return 2 * 6371.0 * asin(sqrt(h))


def _km(leg: str, distances: dict):
    got = distances.get(leg)
    if got is None:
        a, b = leg.split("-")
        got = distances.get(f"{b}-{a}")     # a reverse leg is the same length
    return got


def _weight_km(leg: str, distances: dict):
    """The table's kilometres, else a great-circle estimate, else None."""
    got = _km(leg, distances)
    return got if got is not None else great_circle_km(*leg.split("-"))


@dataclass
class LegRevenue:
    #: {'DAC-CGP': {'2026-07': 12_345_678.0, ...}}
    by_leg: dict = field(default_factory=dict)
    tickets: int = 0
    #: Tickets split across two or more legs.
    split: int = 0
    #: Of those, a leg missing from the distance table, so weighted by a
    #: great-circle estimate for that leg.
    estimated: int = 0
    #: Of those, split equally because not even an estimate was possible.
    equal_split: int = 0
    #: Rows whose routing could not be read, left out rather than guessed.
    unreadable: int = 0

    def for_leg(self, leg: str) -> dict:
        return self.by_leg.get(leg.upper(), {})


def split_revenue(rows, distances: dict | None = None) -> LegRevenue:
    """Apportion each ticket's base fare to its legs, by distance.

    `rows` yield (routing, flight_date, base_fare). A one-leg ticket needs
    no apportioning; a longer one is split in proportion to each leg's
    kilometres.
    """
    distances = distances or {}
    out = LegRevenue()
    acc = defaultdict(lambda: defaultdict(float))
    for routing, when, base in rows:
        if base is None or when is None:
            continue
        legs = legs_of(routing)
        if not legs:
            out.unreadable += 1
            continue
        out.tickets += 1
        month = f"{when:%Y-%m}"
        base = float(base)
        if len(legs) == 1:
            acc[legs[0]][month] += base
            continue
        out.split += 1
        if any(_km(leg, distances) is None for leg in legs):
            out.estimated += 1
        kms = [_weight_km(leg, distances) for leg in legs]
        if all(k and k > 0 for k in kms):
            total = sum(kms)
            shares = [k / total for k in kms]
        else:
            out.equal_split += 1
            shares = [1.0 / len(legs)] * len(legs)
        for leg, share in zip(legs, shares):
            acc[leg][month] += base * share
    out.by_leg = {leg: dict(months) for leg, months in acc.items()}
    return out


def read_warehouse(source, first, last, distances: dict | None = None,
                   ) -> LegRevenue:
    """Base fare per leg per month for flights between `first` and `last`."""
    from . import counter_reconcile as cr
    from .sales_movement import _quoted

    duckdb = cr._duckdb()
    if duckdb is None:
        raise ValueError("duckdb is not available, so base fare cannot be "
                         "read from the warehouse.")
    target = (source.path.as_posix() if source.kind == "gold"
              else source.path.as_posix() + "/**/*.parquet").replace("'", "''")
    con = duckdb.connect()
    con.execute("SET enable_progress_bar=false")
    rows = con.execute(f"""
        select "Routing information",
               cast("Flight date" as date),
               "Total without taxe (base currency)"
        from read_parquet('{target}')
        where "Transaction" in ({_quoted(REVENUE_LINES)})
          and cast("Flight date" as date) between DATE '{first}'
                                              and DATE '{last}'
    """).fetchall()
    return split_revenue(rows, distances)
