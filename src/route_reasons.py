"""Why a schedule change is suggested, in words, from the numbers.

A flag on its own ("FULL AND OUT-FLOWN") invites the question "says who?".
Each suggestion here carries the evidence that produced it, one line per
source, so the sheet can be read by someone who never saw the method:

* demand   -- how full our own aircraft go (the load workbook)
* supply   -- who else flies it, how often, on what (the market pull)
* value    -- what a seat-kilometre earns there against the network (RASK)
* aircraft -- whether the fleet already flying has room (the timetable)

Nothing is claimed that the data does not hold. Where a piece is missing
-- no distance, so no RASK; no free aircraft -- the line says so, rather
than the suggestion quietly going ahead without it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from . import fleet_rotation as fr
from . import route_optimisation as ro

#: Share of the weeks seen on which a weekday's slot must be there to count.
MOSTLY = 0.75

DAY_NAMES = {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday",
             "Thu": "Thursday", "Fri": "Friday", "Sat": "Saturday",
             "Sun": "Sunday"}


def _days(days) -> str:
    names = [DAY_NAMES[d] for d in days]
    if len(names) == 7:
        return "every day"
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


@dataclass
class Advice:
    pair: str
    action: str
    #: "add" for more flying, "review" for flying ahead of demand.
    kind: str
    reasons: list = field(default_factory=list)


def _freq(per_day: float) -> str:
    if per_day >= 1.5:
        return f"{per_day:.0f}× daily"
    week = per_day * 7
    if week >= 6.5:
        return "daily"
    return f"{max(1, round(week))}/week"


def _pct(x) -> str:
    return "—" if x is None else f"{x:.0%}"


def _rivals(view, limit: int = 3) -> str:
    parts = []
    for r in view.rivals[:limit]:
        plane = f" {r.aircraft}" if r.aircraft else ""
        parts.append(f"{r.airline} {_freq(r.flights)}{plane} "
                     f"(~{r.seats:.0f} seats/day)")
    return "; ".join(parts)


def rask_ranks(pairs) -> dict:
    """outbound route -> (rank, of how many), best RASK first."""
    known = sorted((p for p in pairs if p.rask), key=lambda p: -p.rask)
    return {p.outbound_route: (i, len(known))
            for i, p in enumerate(known, start=1)}


def _demand(pair) -> str:
    each = ", ".join(f"{v.route} {_pct(v.load_factor)}"
                     for v in pair.directions if v.load_factor is not None)
    full = [v for v in pair.directions
            if (v.load_factor or 0) >= ro.FULL_ENOUGH]
    if len(full) == len(pair.directions):
        tail = ("there are almost no seats left to sell on the flights we "
                "already fly, in either direction.")
    else:
        tail = (f"{full[0].route} is where seats run out; the return has "
                f"room, so the case rests on the {full[0].route} side.")
    return (f"Demand: our flights go out {_pct(pair.load_factor)} full "
            f"({each}) — {tail}")


def _supply(pair) -> str:
    out = pair.outbound or pair.inbound
    ours = (f"We fly {_freq(out.our_flights)} on "
            f"{out.aircraft or 'our aircraft'} "
            f"(~{out.our_seats:.0f} seats/day each way)")
    line = f"Competition: {ours}. "
    if out.rivals:
        line += f"On {out.route}: {_rivals(out)}. "
    share, fshare = pair.seat_share, pair.flight_share
    line += f"We hold {_pct(share)} of the seats"
    if share is not None and fshare is not None and fshare > share + 0.05:
        line += (f" but {_pct(fshare)} of the departures — their aircraft "
                 f"are bigger, so counting flights flatters us")
    return line + "."


def _value(pair, ranks: dict, network_median) -> str:
    rank = ranks.get(pair.outbound_route)
    if not pair.rask or not rank:
        return ("Value: RASK is not computed — the distance is missing. "
                "Fill it in on the Every route sheet and it calculates.")
    i, n = rank
    cmp = ""
    if network_median:
        ratio = pair.rask / network_median
        cmp = (f", {ratio:.1f}× the network median"
               if ratio >= 1.05 else ", below the network median")
    return (f"Value: each seat-km earns {pair.rask:.2f} BDT in base fare — "
            f"{_ordinal(i)} of {n} routes{cmp}. Extra seats here would earn "
            f"at roughly that rate if they filled like the current ones.")


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd",
                                                 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _good_days(check) -> list:
    out = []
    for wd in fr.WEEKDAYS:
        gaps, _, seen, _, _ = check.by_weekday.get(wd, (0, 0, 0, None, 0))
        if seen and gaps / seen >= MOSTLY:
            out.append(wd)
    return out


def _aircraft(check) -> tuple:
    """(line, days a slot is reliably there, days not flown today)."""
    if check is None:
        return ("Aircraft: not checked — no timetable or block time for "
                "this route.", [], [])
    missing = [d for d in fr.WEEKDAYS if d not in check.flown_days]
    good = _good_days(check)
    trip = f"{check.rotation // 60}h{check.rotation % 60:02d}"
    if not good:
        cells = list(check.by_weekday.values())
        seen = sum(c[2] for c in cells) or 1
        some = sum(c[0] for c in cells) / seen
        spare = sum(c[1] for c in cells) / seen
        an = "an" if check.fam[:1] in "AEIOU" else "a"
        line = (f"Aircraft: {an} {check.fam} already flying has the {trip} "
                f"round trip (with 1h15 turns) free on only {some:.0%} of "
                f"days — its gaps at {check.base} are usually shorter.")
        if spare >= MOSTLY:
            line += (f" On {spare:.0%} of days, though, the timetable "
                     f"leaves at least one {check.fam} unused all day. That "
                     f"is likely the standby or one in maintenance — the "
                     f"rotation fits only by using it.")
        else:
            line += " More flying here needs an extra or bigger aircraft."
        return line, good, missing
    starts = [check.by_weekday[d][3] for d in good]
    when = fr._fmt(sorted(starts)[len(starts) // 2])
    planes = max(check.by_weekday[d][4] for d in good)
    who = (f"{planes} aircraft ({check.fam}) are" if planes > 1
           else f"one aircraft ({check.fam}) is")
    days = _days(good)
    return (f"Aircraft: {who} back at {check.base} and idle from about "
            f"{when} on {days} — long enough for the {trip} round trip "
            f"with 1h15 turns, without taking an aircraft off another "
            f"route.", good, missing)


def _action(check, good: list, missing: list) -> str:
    if check is None:
        return "Add capacity — aircraft not checked"
    if not good:
        cells = list(check.by_weekday.values())
        seen = sum(c[2] for c in cells) or 1
        if sum(c[1] for c in cells) / seen >= MOSTLY:
            return ("Add capacity — only with the unused (standby) "
                    "aircraft; confirm with planning")
        return "Add capacity — needs an extra or bigger aircraft"
    fits = [d for d in missing if d in good]
    if missing and len(fits) == len(missing):
        return "Go daily — add " + _days(fits)
    if fits:
        return "Add a flight on " + _days(fits)
    return "Add a rotation on " + _days(good)


def advise(pair, ranks: dict, network_median=None, check=None):
    """The suggestion for one pair, with its reasons -- or None."""
    if pair.squeezed:
        line, good, missing = _aircraft(check)
        advice = Advice(pair.name, _action(check, good, missing), "add")
        advice.reasons = [_demand(pair), _supply(pair),
                          _value(pair, ranks, network_median), line]
        return advice
    lf = pair.load_factor
    if not pair.thin and lf is not None and lf < 0.65:
        advice = Advice(pair.name, "Review capacity", "review")
        advice.reasons = [
            f"Demand: our flights go out only {_pct(lf)} full — about a "
            f"third of the seats fly empty.",
            _supply(pair),
            _value(pair, ranks, network_median),
            "Consider a smaller aircraft or fewer days before adding "
            "anything here.",
        ]
        return advice
    return None


def advise_all(res: ro.Result, checks=()) -> list:
    """Every pair that warrants a change, the strongest cases first."""
    pairs = res.pairs
    ranks = rask_ranks(pairs)
    rasks = [p.rask for p in pairs if p.rask]
    mid = median(rasks) if rasks else None
    by_pair = {c.pair: c for c in checks if c is not None}
    out = []
    for pair in pairs:
        got = advise(pair, ranks, mid, by_pair.get(pair.name))
        if got:
            out.append(got)
    out.sort(key=lambda a: a.kind != "add")
    return out
