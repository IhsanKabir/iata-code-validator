"""Whether the current flying is the right flying.

Fixtures mirror DAC-CCU as it actually is: our ATR three times a week at
92-96% full, IndiGo daily on an A320, Biman daily on a Q400, and a
Singapore 787 routed through Changi that must not be counted as a
competitor on the leg.
"""
from datetime import date

import pytest

from src import route_optimisation as ro


class _Leg:
    def __init__(self, route, cap, taken, when, ac="ATR-72-600",
                 basis="sold"):
        self.leg_route, self.capacity = route, cap
        self.flown = taken if basis == "flown" else None
        self.sold = taken if basis == "sold" else None
        self.flight_date, self.aircraft = when, ac

    @property
    def seats_taken(self):
        return self.flown if self.flown is not None else self.sold

    @property
    def basis(self):
        return "flown" if self.flown is not None else "sold"

    @property
    def load_factor(self):
        t = self.seats_taken
        return (t / self.capacity) if self.capacity and t is not None else None


def _sched(airline, flight, dep, ac, minutes, o="DAC", d="CCU"):
    return {"queried_origin": o, "queried_destination": d, "origin": o,
            "destination": d, "airline": airline, "flight_number": flight,
            "departure": dep, "aircraft": ac, "_minutes": minutes,
            "queried_date": dep[:10]}


def _legs(n=14):
    """Our ATR, three times a week over a fortnight, ~93% full."""
    out = []
    for i in range(n):
        when = date(2026, 9, 1 + i)
        if when.weekday() in (1, 4, 6):          # Tue/Fri/Sun
            out.append(_Leg("DAC-CCU", 72, 67, when))
    return out


def _market():
    rows = []
    for i in range(14):
        day = f"2026-09-{i + 1:02d}"
        rows.append(_sched("6E", "1106", f"{day}T17:35", "Airbus A320", 35))
        rows.append(_sched("BG", "395", f"{day}T17:15", "DH8", 30))
        # a one-stop through Changi: same marketed O&D, not the same leg
        rows.append(_sched("SQ", "447", f"{day}T23:55", "Boeing-787", 1683))
    return rows


# --------------------------------------------------------------------------
# a connecting itinerary is not a competitor on the leg
# --------------------------------------------------------------------------
def test_a_one_stop_through_a_hub_is_not_counted():
    got = ro.build(_legs(), _market(), days_observed=14).by_route("DAC-CCU")
    assert {r.airline for r in got.rivals} == {"6E", "BG"}


def test_the_cutoff_comes_from_the_route_not_a_fixed_number():
    """A 30-minute hop and a six-hour sector need different cutoffs."""
    assert ro._nonstop_cutoff([30, 35, 1683]) == 60
    assert ro._nonstop_cutoff([]) == ro.NONSTOP_CEILING_MINUTES
    # never above the ceiling, however long the shortest leg is
    assert ro._nonstop_cutoff([200, 210]) == ro.NONSTOP_CEILING_MINUTES


# --------------------------------------------------------------------------
# seats, not flights, are what move the answer
# --------------------------------------------------------------------------
def test_seat_share_and_flight_share_disagree_and_seats_are_the_point():
    got = ro.build(_legs(), _market(), days_observed=14).by_route("DAC-CCU")
    assert got.flight_share > got.seat_share
    assert got.seat_share < 0.2            # an ATR against an A320


def test_the_rivals_carry_their_aircraft_and_the_basis_of_the_count():
    got = ro.build(_legs(), _market(), days_observed=14).by_route("DAC-CCU")
    indigo = next(r for r in got.rivals if r.airline == "6E")
    assert indigo.seats == pytest.approx(180.0)
    assert indigo.seat_basis == "airline"


def test_the_top_rival_is_the_one_with_the_seats_not_the_flights():
    """Biman flies more often; IndiGo carries more people."""
    got = ro.build(_legs(), _market(), days_observed=14).by_route("DAC-CCU")
    assert got.top_rival.airline == "6E"


# --------------------------------------------------------------------------
# the verdict
# --------------------------------------------------------------------------
def test_full_and_out_flown_is_the_shape_worth_acting_on():
    got = ro.build(_legs(), _market(), days_observed=14).by_route("DAC-CCU")
    assert got.load_factor > 0.9
    assert got.squeezed is True
    assert "FULL AND OUT-FLOWN" in got.verdict()


def test_a_full_route_we_already_dominate_is_not_a_candidate():
    rows = [_sched("BG", "999", f"2026-09-{i+1:02d}T08:00", "DH8", 30)
            for i in range(2)]
    legs = [_Leg("DAC-CCU", 189, 180, date(2026, 9, 1 + i), "Boeing 737-800")
            for i in range(14)]
    got = ro.build(legs, rows, days_observed=14).by_route("DAC-CCU")
    assert got.seat_share > 0.9
    assert got.squeezed is False
    assert "already holds the market" in got.verdict()


def test_capacity_ahead_of_demand_is_named_too():
    legs = [_Leg("DAC-CCU", 72, 30, date(2026, 9, 1 + i)) for i in range(14)]
    got = ro.build(legs, _market(), days_observed=14).by_route("DAC-CCU")
    assert "ahead of demand" in got.verdict()


def test_a_route_with_too_few_flights_is_not_ranked():
    legs = [_Leg("DAC-CCU", 72, 67, date(2026, 9, 1))]
    got = ro.build(legs, _market(), days_observed=14).by_route("DAC-CCU")
    assert got.thin is True
    assert got.verdict() == "too few flights to judge"


# --------------------------------------------------------------------------
# distance makes a short route and a long one comparable
# --------------------------------------------------------------------------
def test_rask_needs_distance_and_revenue_and_says_nothing_without_them():
    got = ro.build(_legs(), _market(), days_observed=14).by_route("DAC-CCU")
    assert got.rask is None


# --------------------------------------------------------------------------
# RASK over the same completed months on both sides
# --------------------------------------------------------------------------
def _two_months():
    """Our ATR flying Tue/Fri/Sun from 22 Jun to 20 Sep: July and August
    are the only whole months inside that window."""
    out = []
    d = date(2026, 6, 22)
    while d <= date(2026, 9, 20):
        if d.weekday() in (1, 4, 6):
            out.append(_Leg("DAC-CCU", 72, 67, d))
        d = date.fromordinal(d.toordinal() + 1)
    return out


def test_only_whole_months_inside_the_load_window_are_used():
    assert ro.whole_months(date(2026, 6, 22), date(2026, 9, 20)) ==         ["2026-07", "2026-08"]
    assert ro.whole_months(date(2026, 7, 1), date(2026, 7, 31)) == ["2026-07"]
    assert ro.whole_months(date(2026, 7, 2), date(2026, 7, 31)) == []


def test_rask_is_revenue_over_seats_flown_times_km_in_the_same_months():
    legs = _two_months()
    res = ro.build(legs, _market(), days_observed=14,
                   distances={"DAC-CCU": 329.656},
                   revenue={"DAC-CCU": {"2026-07": 9_580_000,
                                        "2026-08": 10_990_000}})
    view = res.by_route("DAC-CCU")
    seats = sum(x.capacity for x in legs
                if f"{x.flight_date:%Y-%m}" in ("2026-07", "2026-08"))
    assert view.rask == pytest.approx(
        (9_580_000 + 10_990_000) / (seats * 329.656))
    assert view.rask_months == ("2026-07", "2026-08")


def test_forward_months_and_refund_tails_do_not_drag_rask_down():
    """Averaging every month the revenue file held put Kolkata at 14
    instead of 35: half-sold forward months and negative refund tails from
    before the route was live were counted as ordinary months."""
    clean = {"2026-07": 9_580_000, "2026-08": 10_990_000}
    polluted = dict(clean, **{"2024-12": -100_000, "2025-05": 3_880_000,
                              "2026-11": 280_000, "2027-02": 30_000})
    a = ro.build(_two_months(), _market(), days_observed=14,
                 distances={"DAC-CCU": 329.656},
                 revenue={"DAC-CCU": clean}).by_route("DAC-CCU").rask
    b = ro.build(_two_months(), _market(), days_observed=14,
                 distances={"DAC-CCU": 329.656},
                 revenue={"DAC-CCU": polluted}).by_route("DAC-CCU").rask
    assert a == pytest.approx(b)


def test_no_whole_month_means_no_rask_and_says_so():
    res = ro.build(_legs(), _market(), days_observed=14,
                   distances={"DAC-CCU": 329.656},
                   revenue={"DAC-CCU": {"2026-09": 9_910_000}})
    assert res.by_route("DAC-CCU").rask is None
    assert any("No complete calendar month" in w for w in res.warnings)


def test_the_months_used_are_stated_on_the_result():
    res = ro.build(_two_months(), _market(), days_observed=14,
                   distances={"DAC-CCU": 329.656},
                   revenue={"DAC-CCU": {"2026-07": 1, "2026-08": 1}})
    assert any("2026-07, 2026-08" in w for w in res.warnings)


def test_an_unknown_aircraft_is_excluded_and_reported():
    rows = _market() + [_sched("OD", "1", "2026-09-01T09:00", "Airbus-33F", 40)]
    res = ro.build(_legs(), rows, days_observed=14)
    assert res.unknown_aircraft
    assert any("guessed one" in w for w in res.warnings)
    assert "OD" not in {r.airline for r in res.by_route("DAC-CCU").rivals}


def test_the_floor_caveat_is_always_stated():
    res = ro.build(_legs(), _market(), days_observed=14)
    assert any("is a floor" in w for w in res.warnings)


def test_a_sold_basis_is_declared():
    res = ro.build(_legs(), _market(), days_observed=14)
    assert res.basis == "sold"
    assert any("before no-shows" in w for w in res.warnings)
