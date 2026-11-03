"""A stale route list must not hide a fare basis from any route read.

The route list says which fare bases have fares on which route; built once,
it misses fare bases added or moved since. Quick Sale (what is on sale on
the route) and the penalty screen (what has penalties there) fill the gap
on every run, and the list learns from the fare rows read.
"""
from src import pricing_check as pc
from src import pricing_discover as pd
from src import pricing_lookup as pl
from tests.test_pricing_check import TODAY, FakeZenith, same_both_seasons
from tests.test_pricing_lookup import SUMMER, zenith


def stale_index(tmp_path):
    """Knows YBDCCUO on DAC-CCU, not YBDCCUR (added since)."""
    idx = pc.RouteIndex(tmp_path / "routes.json")
    idx.legs = {11: {"DAC-CCU"}}
    idx.built = "2026-09-01 10:00"
    idx.save()
    return idx


def test_the_season_check_reads_a_fare_basis_the_route_list_misses(tmp_path):
    z = same_both_seasons(FakeZenith())
    z.fares[(12, 34199)][0]["AmountRT"] = 550               # changed RT
    idx = stale_index(tmp_path)
    res = pc.run_check(z, idx, pc.Settings(
        routes_text="DAC-CCU", include_return=False, quick_sale=False,
        taxes=False, penalties=False), today=TODAY)
    # YBDCCUR was found in Quick Sale, so its fares were compared
    assert any(f.part == "Fares" and "YBDCCUR" in f.item
               for f in res.findings)
    assert any("YBDCCUR (Quick Sale)" in n for n in res.notes)
    assert any("days ago" in p for p in res.problems)
    # and the route list learned it from the fare rows read
    again = pc.RouteIndex(tmp_path / "routes.json")
    again.load()
    assert again.legs[12] == {"DAC-CCU"}


def test_a_route_lookup_adds_and_learns_what_the_list_misses(tmp_path):
    idx = stale_index(tmp_path)
    res = pl.run_lookup(zenith(), idx, pl.LookupSettings(
        routes_text="DAC-CCU", include_return=False, season_ids=[SUMMER],
        penalties=False, taxes=False, quick_sale=False), today=TODAY)
    via = {f["Code"]: f["Found via"] for f in res.fare_bases}
    assert via == {"YBDCCUO": "Quick Sale, route list",
                   "YBDCCUR": "Quick Sale"}
    assert any(f["Fare basis"] == "YBDCCUR" for f in res.fares)
    again = pc.RouteIndex(tmp_path / "routes.json")
    again.load()
    assert again.legs[12] == {"DAC-CCU"}


def test_a_fare_basis_lookup_does_not_go_looking(tmp_path):
    z = zenith()
    res = pl.run_lookup(z, stale_index(tmp_path), pl.LookupSettings(
        fare_bases_text="YBDCCUO", season_ids=[SUMMER], penalties=False,
        taxes=False, quick_sale=False), today=TODAY)
    assert [f["Code"] for f in res.fare_bases] == ["YBDCCUO"]
    assert "Found via" not in res.fare_bases[0]
    assert not any(str(c).startswith("FDI") for c in z.sent)


def test_learning_only_adds_and_needs_a_list():
    idx = pc.RouteIndex("unused.json")
    assert pd.learn(idx, {1: {"DAC-CCU"}}) == 0          # no list: untouched
    assert idx.legs == {}


def test_learning_adds_routes_and_saves(tmp_path):
    idx = stale_index(tmp_path)
    assert pd.learn(idx, {11: {"DAC-CCU", "DAC-DXB"}, 12: {"DAC-CCU"}}) == 2
    assert pd.learn(idx, {11: {"DAC-CCU"}}) == 0          # nothing new
    assert idx.legs[11] == {"DAC-CCU", "DAC-DXB"}
