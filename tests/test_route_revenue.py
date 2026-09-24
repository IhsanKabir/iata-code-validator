"""Base fare per leg, out of a warehouse that stores one row per ticket.

Each fixture mirrors a real row: the whole fare on one line, labelled with
the first leg, and the routing naming every leg the fare actually pays for.
"""
from datetime import date

import pytest

from src import route_revenue as rr

KM = {"DAC-CGP": 290.764, "CGP-DAC": 325.952, "DAC-DXB": 3564.0,
      "DXB-DAC": 3564.0}
JUL = date(2026, 7, 7)


def test_a_routing_splits_into_legs():
    assert rr.legs_of("DAC-CGP-DAC") == ["DAC-CGP", "CGP-DAC"]
    assert rr.legs_of("CGP-DAC-DXB") == ["CGP-DAC", "DAC-DXB"]
    assert rr.legs_of("DAC-CGP") == ["DAC-CGP"]


def test_an_unreadable_routing_is_not_a_leg():
    for bad in ("", None, "DAC", "DAC-", "DAC-CG", "12-34"):
        assert rr.legs_of(bad) == []


def test_a_one_way_fare_goes_wholly_to_its_leg():
    got = rr.split_revenue([("DAC-CGP", JUL, 5_000)], KM)
    assert got.for_leg("DAC-CGP") == {"2026-07": 5_000}
    assert got.split == 0


def test_a_return_is_split_across_both_directions_not_given_to_the_first():
    """One row, DAC to CGP, 14,349 base -- with nothing against CGP-DAC.
    Summing by departure airport credited the whole fare outbound."""
    got = rr.split_revenue([("DAC-CGP-DAC", JUL, 14_349)], KM)
    out = got.for_leg("DAC-CGP")["2026-07"]
    back = got.for_leg("CGP-DAC")["2026-07"]
    assert out + back == pytest.approx(14_349)
    assert back > 0
    # by distance, not half each: the two directions differ in the table
    assert out == pytest.approx(14_349 * 290.764 / (290.764 + 325.952))


def test_a_connecting_fare_is_not_credited_to_its_short_first_leg():
    """CGP-DAC-DXB carried 31.11M in two months, most of it Dubai. All of
    it would otherwise have landed on a 290 km domestic hop."""
    got = rr.split_revenue([("CGP-DAC-DXB", JUL, 40_000)], KM)
    domestic = got.for_leg("CGP-DAC")["2026-07"]
    longhaul = got.for_leg("DAC-DXB")["2026-07"]
    assert domestic < 0.1 * 40_000
    assert domestic + longhaul == pytest.approx(40_000)


def test_a_reverse_leg_uses_the_distance_of_its_opposite():
    got = rr.split_revenue([("DXB-DAC-CGP", JUL, 10_000)],
                           {"DAC-DXB": 3564.0, "DAC-CGP": 290.764})
    assert got.equal_split == 0


def test_an_unknown_distance_falls_back_to_an_equal_split_and_is_counted():
    got = rr.split_revenue([("DAC-XYZ-DAC", JUL, 10_000)], KM)
    assert got.equal_split == 1
    assert got.for_leg("DAC-XYZ")["2026-07"] == pytest.approx(5_000)


def test_refunds_come_off_through_the_same_split():
    got = rr.split_revenue([("DAC-CGP-DAC", JUL, 14_349),
                            ("DAC-CGP-DAC", JUL, -14_349)], KM)
    assert got.for_leg("DAC-CGP")["2026-07"] == pytest.approx(0)
    assert got.for_leg("CGP-DAC")["2026-07"] == pytest.approx(0)


def test_a_fare_is_filed_under_the_month_of_its_first_flight():
    got = rr.split_revenue([("DAC-CGP", date(2026, 8, 31), 5_000)], KM)
    assert "2026-08" in got.for_leg("DAC-CGP")


def test_unreadable_rows_are_counted_not_guessed():
    got = rr.split_revenue([("", JUL, 5_000), ("DAC-CGP", JUL, 5_000)], KM)
    assert got.unreadable == 1 and got.tickets == 1


def test_rows_with_no_fare_or_date_are_skipped():
    got = rr.split_revenue([("DAC-CGP", JUL, None),
                            ("DAC-CGP", None, 5_000)], KM)
    assert got.tickets == 0
