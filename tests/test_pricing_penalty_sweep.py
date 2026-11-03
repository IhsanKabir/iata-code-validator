"""Penalties on a route come from the penalty screen, not the route list.

The case that showed it (ex-can HAR, 2026-11-03): 24 EX-CAN fare classes
have penalties on leg CAN-MLE, while the route list -- built from the Fare
Editor -- places them on CAN-DAC / CAN-CCU only. A route lookup must still
list them; a fare basis with no penalty must say so, not vanish.
"""
import pytest

from src import pricing_check as pc
from src import pricing_client as cl
from src import pricing_lookup as pl
from src import pricing_parse as pp
from tests.test_pricing_check import BASE, SEASONS, TODAY, penalty_results
from tests.test_pricing_lookup import SUMMER, WINTER, zenith

#: id -> (code, description as results show it, enabled)
CLASSES = {11: ("YBDCCUO", "Y one way", True),
           12: ("YBDCCUR", "Y return", True),
           13: ("OLDX", "X - old | off", False),
           1609: ("BCANO", "B - Economy Flex | EX-CAN SITI OW", True)}
LEGS = {68: "DAC - CCU", 69: "CCU - DAC"}


def sweep_page():
    rows = "".join(
        f'<tr><td><div class="check-bottom"><input type="checkbox" '
        f'data-idClass="{i}" class="cbIDClasse" name="{i}_IDClasse" '
        f'id="{i}_IDClasse"><label for="{i}_IDClasse"><span>{code}    '
        f'</span></label></div></td><td align="left">{text} (Modifiable '
        f'with fee / Refundable with fee)</td></tr>'
        for i, (code, text, _on) in CLASSES.items())
    rows += "".join(
        f'<input type="checkbox" data-idLeg="{i}" name="{i}_IDLeg" '
        f'id="{i}_IDLeg"><label for="{i}_IDLeg"><span>{leg}</span></label>'
        for i, leg in LEGS.items())
    rows += "".join(
        f'<input type="checkbox" name="{s["id"]}_IDPeriode" id="p{s["id"]}">'
        f'<label for="p{s["id"]}"><span>{s["group"]}</span></label>'
        for s in SEASONS)
    rows += ('<input type="checkbox" name="1_IDTypePassager" id="t1">'
             '<label for="t1"><span>AD - Adult(s)</span></label>')
    return f"<table>{rows}</table>"


class SweepZenith:
    """Wraps the look-up stand-in; its penalty search answers only for
    the classes, legs and seasons ticked -- as the live screen does."""

    def __init__(self, base, penalties):
        self.base = base
        self.penalties = penalties      # (class id, leg id, season, mod, ref)
        self.searches = []

    def __getattr__(self, name):
        return getattr(self.base, name)

    def fare_basis_list(self):
        rows = self.base.fare_basis_list()
        rows.append({"IdFareBasis": 1609, "Code": "BCANO",
                     "Text": "B - Economy Flex | EX-CAN SITI OW",
                     "IsEnabled": True, "BookingClassCode": "B",
                     "Level": 8, "MinStay": 0, "MaxStay": 360})
        return rows

    def penalty_page(self):
        return sweep_page()

    def penalty_search(self, form):
        cl.check_allowed("POST", BASE + cl.PENALTIES, form)
        ticked = {int(k.split("_")[0]) for k in form if k.endswith("_IDClasse")}
        self.searches.append(ticked)
        seasons = {s["id"]: s["group"] for s in SEASONS}
        hits = [p for p in self.penalties
                if p[0] in ticked and f"{p[1]}_IDLeg" in form
                and f"{p[2]}_IDPeriode" in form]
        return penalty_results([
            (CLASSES[c][1], LEGS[leg], "Adult(s)", seasons[s],
             "0 - 72 hour(s) before flight departure", mod, ref)
            for c, leg, s, mod, ref in hits])


def route_index(tmp_path, built="2026-10-05 16:58"):
    idx = pc.RouteIndex(tmp_path / "routes.json")
    idx.legs = {11: {"DAC-CCU"}, 12: {"DAC-CCU"}, 1609: {"CAN-DAC"}}
    idx.built = built
    idx.save()
    return idx


PENALTIES = [(11, 68, SUMMER, "300 USD", "480 USD"),
             (1609, 68, SUMMER, "50 USD", "80 USD"),     # not in route list
             (13, 68, SUMMER, "1 USD", "1 USD")]         # disabled class


def lookup(tmp_path, **kw):
    z = SweepZenith(zenith(), PENALTIES)
    kw.setdefault("season_ids", [SUMMER])
    res = pl.run_lookup(z, route_index(tmp_path), pl.LookupSettings(
        include_return=False, taxes=False, quick_sale=False, **kw),
        today=TODAY)
    return z, res


def test_the_penalty_page_names_each_class_by_its_description():
    opts = pp.parse_penalty_page(sweep_page())
    assert opts.class_texts[1609] == "B - Economy Flex | EX-CAN SITI OW"
    assert opts.code_of_text()["B - Economy Flex | EX-CAN SITI OW"] == "BCANO"


def test_a_route_lookup_lists_every_class_with_penalties_on_it(tmp_path):
    z, res = lookup(tmp_path, routes_text="DAC-CCU")
    got = {(p["Fare basis"], p.get("Status", "")) for p in res.penalties}
    assert ("BCANO", "penalty set, but no Fare Editor fare for it on this "
                     "route") in got
    # and BCANO is now a looked-up fare basis, found on the penalty screen
    via = {f["Code"]: f["Found via"] for f in res.fare_bases}
    assert via["BCANO"] == "penalty screen"
    assert "route list" in via["YBDCCUO"]
    assert ("YBDCCUO", "") in got
    assert not any(p["Fare basis"] == "OLDX" for p in res.penalties)
    assert any("BCANO" in n and "missing from the route list" in n
               for n in res.notes)
    # every enabled class asked for in one go, the disabled one never
    assert z.searches == [{11, 12, 1609}]


def test_a_looked_up_fare_basis_without_a_penalty_says_so(tmp_path):
    _z, res = lookup(tmp_path, routes_text="DAC-CCU")
    none = [p for p in res.penalties if p["Fare basis"] == "YBDCCUR"]
    assert [p["Status"] for p in none] == [
        "NO PENALTY SET in Zenith for this route and season"]


def test_a_fare_basis_lookup_still_asks_only_for_its_own_classes(tmp_path):
    z, res = lookup(tmp_path, fare_bases_text="YBDCCUO")
    assert z.searches == [{11}]
    assert {p["Fare basis"] for p in res.penalties} == {"YBDCCUO"}


def test_an_old_route_list_is_flagged(tmp_path):
    z = SweepZenith(zenith(), PENALTIES)
    res = pl.run_lookup(z, route_index(tmp_path, built="2026-09-01 10:00"),
                        pl.LookupSettings(routes_text="DAC-CCU",
                                          include_return=False,
                                          season_ids=[SUMMER], taxes=False,
                                          quick_sale=False), today=TODAY)
    assert any("days ago" in n and "Rebuild route list" in n
               for n in res.notes)
    idx = route_index(tmp_path, built=f"{TODAY:%Y-%m-%d} 09:00")
    assert idx.age_note(TODAY) == ""


def test_the_season_check_asks_for_every_enabled_class(tmp_path):
    z = SweepZenith(zenith(), PENALTIES + [
        (1609, 68, WINTER, "60 USD", "80 USD")])
    res = pc.run_check(z, route_index(tmp_path), pc.Settings(
        routes_text="DAC-CCU", include_return=False, fares=False,
        quick_sale=False, taxes=False), today=TODAY)
    assert {11, 12, 1609} in z.searches and not any(13 in s
                                                    for s in z.searches)
    changed = [(f.item, f.value_a, f.value_b) for f in res.findings
               if f.part == "Penalties"]
    assert any("EX-CAN" in item and a == "50 USD" and b == "60 USD"
               for item, a, b in changed)


def test_the_season_check_reads_penalties_without_a_route_list(tmp_path):
    z = SweepZenith(zenith(), PENALTIES)
    res = pc.run_check(z, pc.RouteIndex(tmp_path / "none.json"), pc.Settings(
        routes_text="DAC-CCU", include_return=False, fares=False,
        quick_sale=False, taxes=False), today=TODAY)
    assert res.compared.get("Penalties", 0) or res.findings
    with pytest.raises(ValueError, match="route list"):
        pc.run_check(z, pc.RouteIndex(tmp_path / "none.json"), pc.Settings(
            routes_text="DAC-CCU", include_return=False, quick_sale=False,
            taxes=False, penalties=False), today=TODAY)


# ---- never a false "no penalty" ------------------------------------------------

def test_a_class_that_did_not_load_is_not_called_penalty_free(tmp_path):
    class Broken(SweepZenith):
        def penalty_search(self, form):
            html = super().penalty_search(form)
            if "12_IDClasse" in form:
                return html.replace("</html>", "")        # cut off: Incomplete
            return html
    z = Broken(zenith(), PENALTIES)
    res = pl.run_lookup(z, route_index(tmp_path), pl.LookupSettings(
        fare_bases_text="YBDCCUO YBDCCUR", include_return=False,
        season_ids=[SUMMER], taxes=False, quick_sale=False), today=TODAY)
    assert not any(p.get("Fare basis") == "YBDCCUR" and "NO PENALTY"
                   in p.get("Status", "") for p in res.penalties)
    assert any("did not load" in n for n in res.notes)


def test_unmatched_rows_stop_no_penalty_claims_on_that_route(tmp_path):
    z = SweepZenith(zenith(), PENALTIES)
    # the screen's list names BCANO differently from its result rows, and
    # BCANO is not a looked-up fare basis: its rows cannot be matched
    z.penalty_page = lambda: sweep_page().replace(
        "B - Economy Flex | EX-CAN SITI OW (", "Something else (")
    res = pl.run_lookup(z, route_index(tmp_path), pl.LookupSettings(
        routes_text="DAC-CCU", include_return=False, season_ids=[SUMMER],
        taxes=False, quick_sale=False), today=TODAY)
    assert any(p["Fare basis"] == "" for p in res.penalties)
    assert not any("NO PENALTY" in p.get("Status", "") for p in res.penalties)
    assert any("could not be matched" in n for n in res.notes)


def test_a_near_match_is_used_only_when_it_is_the_only_one():
    from src import pricing_lookup as lk
    assert lk._code_for("Y one", {"Y one way": "A", "Y one way back": "B"})         == ""
    assert lk._code_for("Y one way back x", {"Y one way back": "B"}) == "B"


def test_a_shared_description_maps_to_no_code():
    opts = pp.PenaltyOptions(classes={1: "AAA", 2: "BBB", 3: "CCC"},
                             class_texts={1: "Same", 2: "Same", 3: "Own"})
    assert opts.code_of_text() == {"Own": "CCC"}
