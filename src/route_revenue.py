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
themselves. Where a leg's distance is unknown the ticket is split equally
instead, and counted, so the report can say how much rests on the cruder
split. Refunds and voids carry negative base fare and go through the same
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


def _km(leg: str, distances: dict):
    got = distances.get(leg)
    if got is None:
        a, b = leg.split("-")
        got = distances.get(f"{b}-{a}")     # a reverse leg is the same length
    return got


@dataclass
class LegRevenue:
    #: {'DAC-CGP': {'2026-07': 12_345_678.0, ...}}
    by_leg: dict = field(default_factory=dict)
    tickets: int = 0
    #: Tickets split across two or more legs.
    split: int = 0
    #: Of those, split equally because a leg's distance was not known.
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
        kms = [_km(leg, distances) for leg in legs]
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
