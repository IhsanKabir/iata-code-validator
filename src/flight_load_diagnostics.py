"""Per-flight load-factor diagnostics from ModificationHistory logs.

Answers "why did this flight fill the way it did?" from a single Zenith
ModificationHistory .xls (already parsed into HistoryEvent records). For each
flight it derives, using only the event stream (no network, no fare data):

  * flown / checked-in / boarded / no-show counts (triple-checkable)
  * cancellations and held-then-cancelled ("Option->Cancelled") coupons
  * hold duration of those held coupons, via a coupon-ID creation clock
    (coupon IDs are assigned sequentially at booking, so ID -> creation time)
  * the booking demand curve (new PNRs by weeks before departure)
  * fare-class mix of the flown coupons
  * an estimated capacity (seat-map max row x 6) + load factor
  * a set of plain-language FLAGS explaining the flight's behaviour

`compare_flights` then adds route-relative flags (e.g. a flight well below its
route's typical flown count). Everything is immutable (frozen dataclasses with
tuples) so results are safe to pass between threads / into the Excel writer.
"""

from __future__ import annotations

import bisect
import glob
import os
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .zenith_history_parser import HistoryEvent, parse_flight, parse_history_file

# --- thresholds (named, not magic) ------------------------------------------
_EPOCH = datetime(2026, 1, 1)
LOW_LF_PCT = 55.0              # below this estimated LF -> LOW_LF flag
VERY_LOW_LF_PCT = 40.0
LATE_CURVE_PCT = 40.0         # >= this share of bookings in the final week
HIGH_NOSHOW_PCT = 5.0         # no-shows as % of flown
HIGH_CANCEL_PCT = 15.0        # segment-cancelled PNRs as % of all PNRs
HIGH_HELD_CHURN = 0.6         # held coupons / flown
BELOW_ROUTE_NORM = 0.75       # flown < this * route-median flown


@dataclass(frozen=True)
class FlightDiagnostics:
    """One flight's load-factor diagnosis. All counts are exact; capacity/LF
    are estimates (seat-map based) and labelled as such downstream."""

    source_name: str
    flight_number: str
    route: str                       # "DAC-BKK"
    flight_date: str                 # DD/MM/YYYY
    departure_time: str              # HH:MM
    total_pnrs: int
    flown: int
    flown_pnrs: int
    checked_in: int
    boarded: int
    no_shows: int
    cancellations: int               # segment-cancelled PNRs
    held_pnrs: int
    held_coupons: int
    held_median_hold_h: float
    held_max_hold_h: float
    held_pct_under_24h: float
    capacity_est: int
    load_factor_est: float           # %, estimate
    clock_error_h: float             # leave-one-out p90 of the creation clock
    pct_booked_final_week: float
    fare_class_mix: tuple[tuple[str, int], ...]
    demand_curve: tuple[tuple[int, int, int], ...]   # (weeks_before, new, cumulative)
    channel_mix: tuple[tuple[str, int], ...]
    flags: tuple[str, ...]


FLAG_REASONS: dict[str, str] = {
    "VERY_LOW_LF": "Estimated load factor under 40% — flight flew largely empty.",
    "LOW_LF": "Estimated load factor under 55%.",
    "LATE_CURVE": "No advance base — a large share of bookings arrived in the final week.",
    "HIGH_NOSHOW": "No-shows are an unusually high share of flown passengers.",
    "HIGH_CANCEL": "A high share of PNRs cancelled their segment.",
    "HIGH_HELD_CHURN": "Many coupons were held then dropped (heavy GDS quote churn).",
    "BELOW_ROUTE_NORM": "Flew well below this route's typical passenger count.",
    "NO_SEAT_MAP": "No seat-map data — capacity/LF could not be estimated.",
}


def _is_qdcs(s: str) -> bool:
    s = s or ""
    return any(k in s for k in (
        "QDCS", "Check-in", "Boarding of", "Seat modification", "Luggage",
        "boarding pass", "Print boarding"))


def _dominant_flight(events: list[HistoryEvent]):
    """The flight this log is mostly about: the most common parsed flight cell."""
    counts: Counter = Counter()
    for e in events:
        fr = parse_flight(e.raw_flight or "")
        if fr.flight_number and fr.origin and fr.destination:
            counts[(fr.flight_number, fr.origin, fr.destination,
                    fr.flight_date, fr.departure_time)] += 1
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def _departure_dt(flight_date: str, dep_time: str) -> datetime | None:
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(f"{flight_date} {dep_time}".strip(), fmt)
        except ValueError:
            continue
    return None


def _creation_clock(events: list[HistoryEvent], cancelled_ids: set[int]):
    """coupon_id -> earliest booking-event time, for NON-cancelled coupons.

    Coupon IDs are globally sequential at creation, so this (id, time) curve is
    a creation clock. Returns (xs, ys_days, p90_error_hours)."""
    book_min: dict[int, datetime] = {}
    for e in events:
        if not e.timestamp or _is_qdcs(e.raw_description):
            continue
        ids = {int(c) for c in re.findall(r"\[(\d+)\]", e.raw_description or "")}
        m = re.search(r"change:(\d+)", e.raw_description or "")
        if m:
            ids.add(int(m.group(1)))
        for cid in ids:
            if cid in cancelled_ids:
                continue
            if cid not in book_min or e.timestamp < book_min[cid]:
                book_min[cid] = e.timestamp
    pts = sorted(book_min.items())
    xs = [c[0] for c in pts]
    ys = [(c[1] - _EPOCH).total_seconds() / 86400 for c in pts]
    err = _leave_one_out_p90(xs, ys) if len(xs) >= 5 else 0.0
    return xs, ys, err


def _interp(xs: list[int], ys: list[float], cid: int) -> float:
    if not xs:
        return 0.0
    j = bisect.bisect_left(xs, cid)
    if j <= 0:
        return ys[0]
    if j >= len(xs):
        return ys[-1]
    x0, x1, y0, y1 = xs[j - 1], xs[j], ys[j - 1], ys[j]
    return y0 if x1 == x0 else y0 + (y1 - y0) * (cid - x0) / (x1 - x0)


def _leave_one_out_p90(xs: list[int], ys: list[float]) -> float:
    errs = []
    for i in range(len(xs)):
        xs2, ys2 = xs[:i] + xs[i + 1:], ys[:i] + ys[i + 1:]
        errs.append(abs(ys[i] - _interp(xs2, ys2, xs[i])))
    errs.sort()
    return errs[int(0.9 * len(errs))] * 24 if errs else 0.0


def _hold_durations(events, by_pnr, cancelled_ids, clock) -> list[float]:
    """Hours each held-then-cancelled coupon was held = cancel_time - clock(id)."""
    xs, ys, _ = clock
    holds: list[float] = []
    for p, es in by_pnr.items():
        txt = " ".join(x.raw_description or "" for x in es)
        if ("Option->Cancelled" not in txt or "New status:Flown" in txt
                or "Segment Cancelled" in txt):
            continue
        for e in es:
            if "Option->Cancelled" not in (e.raw_description or "") or not e.timestamp:
                continue
            for cid in re.findall(r"\[(\d+)\]", e.raw_description):
                h = ((e.timestamp - _EPOCH).total_seconds() / 86400
                     - _interp(xs, ys, int(cid))) * 24
                holds.append(max(0.0, h))
    return holds


def diagnose_flight(events: list[HistoryEvent], *, source_name: str) -> FlightDiagnostics | None:
    """Diagnose one flight's load behaviour from its parsed history events.

    Returns None if the log has no flown passengers (e.g. a not-yet-operated
    flight) — those carry no load story."""
    if not events:
        return None
    dom = _dominant_flight(events)
    flight_number = dom[0] if dom else ""
    route = f"{dom[1]}-{dom[2]}" if dom else ""
    flight_date = dom[3] if dom else ""
    dep_time = dom[4] if dom else ""

    flown_ids = re.findall(
        r"Coupon status change:(\d+) Old status:Boarded New status:Flown",
        " ".join(e.raw_description or "" for e in events))
    flown = len(set(flown_ids))
    if flown == 0:
        return None

    blob = " ".join(e.raw_description or "" for e in events)
    checked_in = len(set(re.findall(r"Check-in of the coupon (\d+)", blob)))
    boarded = len(set(re.findall(
        r"Boarding of the coupon (\d+) : Previous status = CK , New status = BD", blob)))
    no_shows = len(set(re.findall(r"No Show of the coupon (\d+)", blob)))

    by_pnr: dict[str, list] = defaultdict(list)
    for e in events:
        if e.pnr:
            by_pnr[e.pnr].append(e)
    flown_pnrs = {p for p, es in by_pnr.items()
                  if "New status:Flown" in " ".join(x.raw_description or "" for x in es)}
    cancel_pnrs = {p for p, es in by_pnr.items()
                   if "Segment Cancelled" in " ".join(x.raw_description or "" for x in es)}

    cancelled_ids = {int(c) for e in events if "Option->Cancelled" in (e.raw_description or "")
                     for c in re.findall(r"\[(\d+)\]", e.raw_description)}
    held_pnrs = [p for p, es in by_pnr.items()
                 if "Option->Cancelled" in " ".join(x.raw_description or "" for x in es)
                 and "New status:Flown" not in " ".join(x.raw_description or "" for x in es)
                 and "Segment Cancelled" not in " ".join(x.raw_description or "" for x in es)]
    clock = _creation_clock(events, cancelled_ids)
    holds = _hold_durations(events, by_pnr, cancelled_ids, clock)
    held_med = statistics.median(holds) if holds else 0.0
    held_max = max(holds) if holds else 0.0
    held_u24 = (100 * sum(1 for h in holds if h < 24) / len(holds)) if holds else 0.0

    # capacity / LF estimate from the occupied seat map
    rows = {int(m) for m in re.findall(r"Seat\(s\)\s*([0-9]{1,2})[A-K]", blob)}
    rows |= {int(m) for m in re.findall(r"Seat row\s*=\s*([0-9]{1,2})", blob)}
    capacity = max(rows) * 6 if rows else 0
    lf = round(100 * flown / capacity, 1) if capacity else 0.0

    # fare-class mix of flown
    cc: dict[str, str] = {}
    for e in events:
        m = re.search(r"\[(\d+)\][^<]*?/([A-Z])\s+[A-Z]{3} [A-Z]{3}", e.raw_description or "")
        if m:
            cc[m.group(1)] = m.group(2)
    fare = Counter(cc.get(c, "?") for c in set(flown_ids))

    # demand curve + channel mix
    dep_dt = _departure_dt(flight_date, dep_time)
    demand: tuple[tuple[int, int, int], ...] = ()
    pct_final_week = 0.0
    if dep_dt and flown_pnrs:
        firstbook = {p: min(x.timestamp for x in by_pnr[p] if x.timestamp)
                     for p in flown_pnrs if any(x.timestamp for x in by_pnr[p])}
        wk = Counter((dep_dt - t).days // 7 for t in firstbook.values())
        rows_out, cum = [], 0
        for w in sorted(wk, reverse=True):
            cum += wk[w]
            rows_out.append((w, wk[w], cum))
        demand = tuple(rows_out)
        if firstbook:
            final = sum(1 for t in firstbook.values() if (dep_dt - t).days < 7)
            pct_final_week = round(100 * final / len(firstbook), 1)

    channel = Counter()
    for p in flown_pnrs:
        es = sorted(by_pnr[p], key=lambda x: (x.timestamp or _EPOCH))
        a = es[0].agent
        channel[a.user_id or a.display_name or "?"] += 1

    flags = _flags(flown, no_shows, len(cancel_pnrs), len(by_pnr),
                   len(holds), lf, capacity, pct_final_week)

    return FlightDiagnostics(
        source_name=source_name, flight_number=flight_number, route=route,
        flight_date=flight_date, departure_time=dep_time, total_pnrs=len(by_pnr),
        flown=flown, flown_pnrs=len(flown_pnrs), checked_in=checked_in,
        boarded=boarded, no_shows=no_shows, cancellations=len(cancel_pnrs),
        held_pnrs=len(held_pnrs), held_coupons=len(holds),
        held_median_hold_h=round(held_med, 1), held_max_hold_h=round(held_max, 1),
        held_pct_under_24h=round(held_u24, 1), capacity_est=capacity,
        load_factor_est=lf, clock_error_h=round(clock[2], 1),
        pct_booked_final_week=pct_final_week,
        fare_class_mix=tuple(fare.most_common()),
        demand_curve=demand, channel_mix=tuple(channel.most_common(8)),
        flags=flags,
    )


def _flags(flown, no_shows, cancels, total_pnrs, held, lf, cap, pct_final_week) -> tuple[str, ...]:
    out: list[str] = []
    if not cap:
        out.append("NO_SEAT_MAP")
    elif lf < VERY_LOW_LF_PCT:
        out.append("VERY_LOW_LF")
    elif lf < LOW_LF_PCT:
        out.append("LOW_LF")
    if pct_final_week >= LATE_CURVE_PCT:
        out.append("LATE_CURVE")
    if flown and 100 * no_shows / flown >= HIGH_NOSHOW_PCT:
        out.append("HIGH_NOSHOW")
    if total_pnrs and 100 * cancels / total_pnrs >= HIGH_CANCEL_PCT:
        out.append("HIGH_CANCEL")
    if flown and held / flown >= HIGH_HELD_CHURN:
        out.append("HIGH_HELD_CHURN")
    return tuple(out)


def inspect_folder(
    folder: str,
    *,
    progress_cb: Callable[[int, int, str], None] | None = None,
) -> list[FlightDiagnostics]:
    """Parse + diagnose every ModificationHistory*.xls in `folder`, then add
    route-relative flags. Files with no flown passengers are skipped. A flaky
    file is skipped (not fatal) so one bad export doesn't sink the whole run."""
    paths = sorted(glob.glob(os.path.join(folder, "*.xls")))
    diags: list[FlightDiagnostics] = []
    for i, p in enumerate(paths, 1):
        name = os.path.basename(p).replace("ModificationHistory ", "").replace(".xls", "")
        if progress_cb:
            progress_cb(i, len(paths), name)
        try:
            d = diagnose_flight(parse_history_file(p), source_name=name)
        except Exception:  # noqa: BLE001 — one bad file must not sink the batch
            d = None
        if d is not None:
            diags.append(d)
    return compare_flights(diags)


def compare_flights(diags: list[FlightDiagnostics]) -> list[FlightDiagnostics]:
    """Add route-relative flags (BELOW_ROUTE_NORM) by comparing flown counts of
    flights on the same route. Returns new FlightDiagnostics (immutable)."""
    from dataclasses import replace
    by_route: dict[str, list[int]] = defaultdict(list)
    for d in diags:
        by_route[d.route].append(d.flown)
    medians = {r: statistics.median(v) for r, v in by_route.items() if len(v) >= 2}
    out = []
    for d in diags:
        med = medians.get(d.route)
        if med and d.flown < BELOW_ROUTE_NORM * med and "BELOW_ROUTE_NORM" not in d.flags:
            out.append(replace(d, flags=d.flags + ("BELOW_ROUTE_NORM",)))
        else:
            out.append(d)
    return out
