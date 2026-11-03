"""The tax setup check, against a stand-in tax screen built like the live
one (rows `<tr data-applicable="...">`, airports named, not coded)."""
import threading

import pytest
from openpyxl import load_workbook

from src import pricing_client as cl
from src import pricing_parse as pp
from src import tax_setup as ts
from src import tax_setup_report as tr

AIRPORTS = [("AUH", 5543, "Abu Dhabi, Zayed International Airport"),
            ("BKK", 5866, "Bangkok"), ("CAN", 6207, "Guangzhou"),
            ("CXB", 6658, "Cox's Bazar"), ("DAC", 6707, "Dhaka"),
            ("CGP", 6450, "Chattogram")]
NAME = {code: name for code, _g, name in AIRPORTS}
CODE = {g: code for code, g, _n in AIRPORTS}
TICK = "&#xE5CA;"


def tax_page():
    opts = "".join(f'<option value="{g}">{c} - {n.replace(chr(39), "&#39;")}'
                   f"</option>" for c, g, n in AIRPORTS)
    return (f'<select name="RecCodeDestGeo"><option value="">-- Airports --'
            f"</option>{opts}</select>")


def tax_row(tax_id, name, code, dep, arr, value, intl=True, dom=False,
            applicable=True):
    """One row as the live page writes it (label first, then the value)."""
    def cell(label, val):
        return (f'<td><div class="visible-xs-inline xs-left">{label}</div>'
                f'<div class="visible-xs-inline xs-right">{val}</div></td>')
    return (f'<tr data-applicable="{str(applicable).lower()}">'
            f'<td class="th-libelle"><div>Tax Name</div><a href="" '
            f'onclick="return Affiche({tax_id});">{name}</a></td>'
            + cell("Tax Code", code)
            + cell("Departure Airport", NAME.get(dep, ""))
            + cell("Arrival Airport", NAME.get(arr, ""))
            + f"<td><span>{value}</span></td>"
            + cell("International", f'<i class="material-icons">{TICK}</i>'
                   if intl else "")
            + cell("Domestic", f'<i class="material-icons">{TICK}</i>'
                   if dom else "")
            + "<td><div>History</div><div>History</div></td></tr>")


def tax_list(rows):
    head = "".join(f"<th>{h}</th>" for h in pp._TAX_HEADER)
    return (f'<table id="SeekResult"><thead><tr>{head}</tr></thead><tbody>'
            + "".join(tax_row(*r) for r in rows) + "</tbody></table>")


class FakeTaxScreen:
    """Tax entries; a search lists every entry touching either airport the
    way the live screen does (both directions, plus 'every route' ones)."""

    def __init__(self, entries):
        self.entries = entries     # (id, name, code, dep, arr, value, ...)
        self.searches = []

    def tax_page(self):
        return tax_page()

    def tax_search(self, form):
        cl.check_allowed("POST", cl.TAXES, form)     # read-only, as live
        a = CODE[int(form["RecCodeDestGeo"])]
        b = CODE[int(form["RecCodeDestGeoLinked"])]
        self.searches.append((a, b, form["RecEffDate"]))
        pair = {a, b}
        hits = [e for e in self.entries
                if (e[3] in pair and e[4] in pair) or
                (e[3] in pair and e[4] == "") or (e[3] == "" and e[4] in pair)]
        return tax_list(hits)


def run(entries, **kw):
    kw.setdefault("date_ddmmyyyy", "03/11/2026")
    z = FakeTaxScreen(entries)
    return z, ts.run_tax_setup_check(z, ts.TaxSetupSettings(**kw))


def kinds(res):
    return [(f.kind, f.route, f.code) for f in res.findings]


# ---- reading the screen --------------------------------------------------------

def test_not_applicable_rows_are_read_as_such():
    taxes = pp.parse_tax_list(tax_list([
        (6117, "Airport Tax", "YR", "AUH", "CAN", "55 USD", False, False,
         False),
        (3348, "Passenger Service Charge | XDAC", "AE", "AUH", "CAN",
         "75 AED")]))
    assert [(t.tax_id, t.applicable, t.international) for t in taxes] == [
        (6117, False, False), (3348, True, True)]
    assert taxes[1].departure == NAME["AUH"] and taxes[0].arrival == "Guangzhou"


def test_a_row_without_the_mark_counts_as_applicable():
    html = tax_list([(1, "Fee", "BD", "DAC", "BKK", "500 BDT")]).replace(
        ' data-applicable="true"', "")
    assert pp.parse_tax_list(html)[0].applicable


def test_the_search_asks_for_not_applicable_taxes_too():
    form = pp.tax_search_form(5543, 6207, "03/11/2026")
    assert form["ShowNotInUseTaxes"] == "on" and form["hAction"] == "Seek"
    cl.check_allowed("POST", cl.TAXES, form)


def test_airport_names_come_with_their_codes():
    listed = pp.parse_tax_airport_list(tax_page())
    assert ("CXB", 6658, "Cox's Bazar") in listed
    assert pp.parse_tax_airports(tax_page())["AUH"] == 5543


# ---- which routes ---------------------------------------------------------------

def test_route_kinds_follow_bangladesh_airports():
    assert [ts.route_kind(*r) for r in (("AUH", "BKK"), ("AUH", "DAC"),
                                        ("CGP", "AUH"), ("DAC", "CXB"))] == \
        ["INT-INT", "INT-DOM", "DOM-INT", "DOM-DOM"]


def test_routes_by_name_pattern_and_return():
    codes = [c for c, _g, _n in AIRPORTS]
    pairs, bad = ts.parse_routes("AUH-BKK, *-CGP AUHCAN XYZ-DAC junk",
                                 codes, include_return=False)
    assert ("AUH", "BKK") in pairs and ("AUH", "CAN") in pairs
    assert {d for d, a in pairs if a == "CGP"} == set(codes) - {"CGP"}
    assert bad == ["XYZ-DAC", "JUNK"]
    back, _ = ts.parse_routes("AUH-BKK", codes, include_return=True)
    assert back == {("AUH", "BKK"), ("BKK", "AUH")}


def test_route_types_filter_the_routes():
    codes = [c for c, _g, _n in AIRPORTS]
    got, _ = ts.pick_routes(codes, ts.TaxSetupSettings(
        routes_text="AUH-*", kinds=("INT-DOM",)))
    assert got == [("AUH", "CGP"), ("AUH", "CXB"), ("AUH", "DAC")]
    every, _ = ts.pick_routes(codes, ts.TaxSetupSettings(kinds=("DOM-DOM",)))
    assert len(every) == 3 * 2
    with pytest.raises(ValueError):
        ts.pick_routes(codes, ts.TaxSetupSettings(routes_text="AUH-BKK",
                                                  kinds=("DOM-DOM",)))


def test_tax_codes_are_checked():
    assert ts.parse_codes("ae, zr E5 ae") == ["AE", "ZR", "E5"]
    with pytest.raises(ValueError):
        ts.parse_codes("AE, YQX")


# ---- the run --------------------------------------------------------------------

AUH = [  # AE and ZR from AUH everywhere but CAN; F6 only to BKK
    (1, "Passenger Service Charge | XDAC", "AE", "AUH", "BKK", "75 AED"),
    (2, "Passenger Service Charge | XDAC", "AE", "AUH", "DAC", "75 AED"),
    (3, "Passenger Service Charge | XDAC", "AE", "AUH", "CGP", "70 AED"),
    (4, "Passenger Service Charge | XDOM", "AE", "AUH", "CGP", "75 AED"),
    (5, "Advance Passenger Fee", "ZR", "AUH", "BKK", "5 AED"),
    (6, "Advance Passenger Fee", "ZR", "AUH", "DAC", "5 AED"),
    (7, "Advance Passenger Fee", "ZR", "AUH", "CGP", "5 AED"),
    (8, "Airport Tax", "YR", "AUH", "CAN", "55 USD", False, False, False),
    (9, "Fuel Surcharge | MLE", "YQ", "AUH", "", "0 %", True, True),
    (10, "Facilities", "F6", "AUH", "BKK", "50 AED"),
    (11, "Departure Fee", "G8", "BKK", "AUH", "25 THB"),
    (12, "Embarkation", "BD", "DAC", "AUH", "500 BDT", False, True),
]


def test_each_pair_is_searched_once_on_the_date_given():
    z, res = run(AUH, routes_text="AUH-*", include_return=True)
    assert res.searches == len(z.searches) == 5
    assert {d for _a, _b, d in z.searches} == {"03/11/2026"}
    assert len(res.routes) == 10


def test_taxes_land_on_the_direction_they_are_set_for():
    _z, res = run(AUH, routes_text="AUH-BKK", include_return=True)
    there = {r.code for r in res.per_route[("AUH", "BKK")]}
    back = {r.code for r in res.per_route[("BKK", "AUH")]}
    assert there == {"AE", "ZR", "YQ", "F6"} and back == {"G8"}


def test_an_every_route_tax_reaches_only_routes_its_ticks_fit():
    entries = [(9, "Fuel Surcharge", "YQ", "AUH", "", "0 %", True, False),
               (1, "Fee", "AE", "AUH", "BKK", "75 AED"),
               (2, "Dom fee", "BD", "DAC", "CXB", "100 BDT", False, True),
               (3, "Int only", "XX", "DAC", "", "1 USD", True, False)]
    _z, res = run(entries, routes_text="AUH-BKK DAC-CXB")
    assert {r.code for r in res.per_route[("AUH", "BKK")]} == {"AE", "YQ"}
    assert [r.code for r in res.per_route[("DAC", "CXB")]] == ["BD"]


def test_the_findings():
    _z, res = run(AUH, routes_text="AUH-*")
    found = kinds(res)
    # CAN has no AE/ZR while the other AUH routes do -- but CAN carries no
    # route tax that applies (YR is marked not applicable), so it is a route
    # with no tax, not a list of missing ones
    assert (ts.NO_ROUTE_TAX, "AUH-CAN", "") in found
    assert (ts.NO_ROUTE_TAX, "AUH-CXB", "") in found
    # AE twice on AUH-CGP (XDAC 70 + XDOM 75): reported as twice, not
    # also as a different amount
    assert (ts.TWICE, "AUH-CGP", "AE") in found
    assert not [f for f in res.findings if f.kind == ts.DIFFERENT]
    assert (ts.NOT_APPLICABLE, "AUH-CAN", "YR") in found
    assert (ts.ZERO, "every route from AUH", "YQ") in found
    # AUH-CGP and AUH-DAC are both INT-DOM and both carry AE and ZR -- fine;
    # F6 on BKK alone is the only INT-INT route taxed, so no peers to judge
    assert not [f for f in res.findings if f.kind == ts.MISSING]


def test_missing_and_only_on_some_routes():
    entries = [(i, "Service", "AE", "AUH", arr, "75 AED")
               for i, arr in enumerate(("BKK", "CAN"), 1)] + [
        (5, "Facilities", "F6", "AUH", "BKK", "50 AED"),
        (6, "Service", "AE", "AUH", "DAC", "75 AED"),
        (7, "Service", "AE", "AUH", "CGP", "75 AED"),
        (8, "Service", "AE", "AUH", "CXB", "75 AED"),
        (9, "Security", "TP", "AUH", "DAC", "5 AED"),
        (10, "Security", "TP", "AUH", "CGP", "5 AED")]
    _z, res = run(entries, routes_text="AUH-*")
    found = kinds(res)
    assert (ts.MISSING, "AUH-CXB", "TP") in found           # 2 of 3 have it
    f6 = [f for f in res.findings if f.code == "F6"]
    assert [(f.kind, f.route) for f in f6] == [(ts.SOME, "AUH-BKK")]
    assert "AUH-CAN" in f6[0].expected


GAPS = [  # INT-DOM from AUH: AE on DAC and CGP, not CXB; F6 on DAC only
    (1, "Service", "AE", "AUH", "DAC", "75 AED"),
    (2, "Service", "AE", "AUH", "CGP", "75 AED"),
    (3, "Security", "ZR", "AUH", "CXB", "5 AED"),
    (4, "Facilities", "F6", "AUH", "DAC", "50 AED"),
    (5, "Security", "ZR", "AUH", "BKK", "5 AED"),
    (6, "Departure Fee", "G8", "BKK", "AUH", "25 THB")]


def test_asking_for_codes_reports_the_routes_without_them():
    _z, res = run(GAPS, codes_text="AE, F6", routes_text="AUH-*",
                  include_return=True)
    found = kinds(res)
    assert (ts.NOT_SET_UP, "AUH-CXB", "AE") in found
    assert (ts.NOT_SET_UP, "AUH-CGP", "F6") in found
    # F6 on one of three INT-DOM routes: Not set up, never ALSO "only on
    # some routes" for the same code
    assert [f.kind for f in res.findings if f.code == "F6"
            and f.route_type == "INT-DOM"] == [ts.NOT_SET_UP, ts.NOT_SET_UP]
    # AUH-BKK is INT-INT: no INT-INT route from AUH has AE -- one line,
    # low down, not "Not set up" judged against the INT-DOM routes
    assert (ts.NOT_AT_AIRPORT, "AUH-BKK", "AE") in found
    assert (ts.NOT_SET_UP, "AUH-BKK", "AE") not in found
    assert (ts.NOT_AT_AIRPORT, "BKK-AUH", "AE") in found
    order = [f.kind for f in res.findings]
    assert order.index(ts.NOT_SET_UP) < order.index(ts.NOT_AT_AIRPORT)


def test_amounts_are_compared_within_a_route_type():
    entries = [(1, "Service", "AE", "AUH", "DAC", "75 AED"),
               (2, "Service", "AE", "AUH", "CGP", "75 AED"),
               (3, "Service", "AE", "AUH", "CXB", "70 AED"),
               (4, "Service", "AE", "AUH", "BKK", "60 AED"),
               (5, "Service", "AE", "AUH", "CAN", "60 AED")]
    _z, res = run(entries, routes_text="AUH-*")
    diff = [(f.route, f.route_type, f.found, f.expected)
            for f in res.findings if f.kind == ts.DIFFERENT]
    # 60 on the INT-INT routes is not judged against 75 on INT-DOM
    assert diff == [("AUH-CXB", "INT-DOM", "70 AED", "75 AED on 2 route(s)")]


def test_a_tax_set_for_every_route_counts_as_present():
    entries = [(1, "Fuel", "YQ", "AUH", "", "10 USD"),
               (2, "Fuel", "YQ", "AUH", "DAC", "12 USD"),
               (3, "Service", "AE", "AUH", "DAC", "75 AED"),
               (4, "Service", "AE", "AUH", "CGP", "75 AED"),
               (5, "Service", "AE", "AUH", "CXB", "75 AED")]
    _z, res = run(entries, routes_text="AUH-*", kinds=("INT-DOM",))
    assert not [f for f in res.findings
                if f.code == "YQ" and f.kind in (ts.MISSING, ts.SOME)]
    assert (ts.TWICE, "AUH-DAC", "YQ") in kinds(res)


def test_a_route_not_fully_read_is_not_called_missing_anything():
    class Odd(FakeTaxScreen):
        def tax_search(self, form):
            html = super().tax_search(form)
            if "6450" in (form["RecCodeDestGeo"], form["RecCodeDestGeoLinked"]):
                html = html.replace("<tbody>", "<tbody>" + tax_row(
                    99, "Odd", "ZZ", "AUH", "CGP", "1 USD").replace(
                    "Abu Dhabi, Zayed International Airport", "Nowhere"))
            return html
    z = Odd([(1, "Service", "AE", "AUH", "DAC", "75 AED"),
             (2, "Service", "AE", "AUH", "CXB", "75 AED")])
    res = ts.run_tax_setup_check(z, ts.TaxSetupSettings(
        routes_text="AUH-DAC AUH-CGP AUH-CXB", codes_text="AE",
        date_ddmmyyyy="03/11/2026"))
    about_cgp = [f.kind for f in res.findings if f.route == "AUH-CGP"]
    assert about_cgp == [ts.NOT_READ]
    assert res.unread == {("AUH", "CGP")}


def test_a_named_route_with_the_wrong_tick_is_reported():
    entries = [(1, "Dom only", "BD", "DAC", "CXB", "100 BDT", True, False),
               (2, "Ok", "XY", "DAC", "CGP", "100 BDT", False, True)]
    _z, res = run(entries, routes_text="DAC-CXB DAC-CGP")
    ticked = [f for f in res.findings if f.kind == ts.NOT_TICKED]
    assert [(f.route, f.expected) for f in ticked] == [
        ("DAC-CXB", "Domestic ticked")]


def test_a_row_on_an_airport_not_searched_is_noted_not_guessed():
    class Odd(FakeTaxScreen):
        def tax_search(self, form):
            return tax_list([(1, "Odd", "ZZ", "CAN", "BKK", "1 USD"),
                             (2, "Fee", "AE", "AUH", "DAC", "75 AED")])
    z = Odd([])
    res = ts.run_tax_setup_check(z, ts.TaxSetupSettings(
        routes_text="AUH-DAC", date_ddmmyyyy="03/11/2026"))
    assert [r.code for r in res.per_route[("AUH", "DAC")]] == ["AE"]
    assert any("ZZ" in n for n in res.notes)


def test_a_bad_date_and_stop():
    with pytest.raises(ValueError):
        run(AUH, routes_text="AUH-BKK", date_ddmmyyyy="2026-11-03")
    stop = threading.Event()
    stop.set()
    with pytest.raises(InterruptedError):
        ts.run_tax_setup_check(FakeTaxScreen(AUH), ts.TaxSetupSettings(
            routes_text="AUH-BKK", date_ddmmyyyy="03/11/2026"), stop=stop)


# ---- the workbook ----------------------------------------------------------------

def test_the_workbook(tmp_path):
    _z, res = run(GAPS, codes_text="AE, F6", routes_text="AUH-*")
    path = tmp_path / tr.default_filename()
    tr.build_workbook(res, path)
    wb = load_workbook(path)
    assert wb.sheetnames == ["About", "Findings", "Coverage", "All taxes"]
    cov = wb["Coverage"]
    head = [c.value for c in cov[4]]
    assert head == ["Route", "Route type", "AE", "F6"]
    rows = {r[0].value: r for r in cov.iter_rows(min_row=5)}
    assert rows["AUH-DAC"][2].value == "75 AED"
    assert rows["AUH-CXB"][2].value == "—"
    assert rows["AUH-CXB"][2].fill.fgColor.rgb.endswith(tr.BAD)
    assert not rows["AUH-BKK"][2].fill.fgColor.rgb.endswith(tr.BAD)
    findings = [r[0].value for r in wb["Findings"].iter_rows(min_row=5)]
    assert ts.NOT_SET_UP in findings
    every = [r for r in wb["All taxes"].iter_rows(min_row=5, values_only=True)]
    assert any("ZR" in r and "this route" in r for r in every)


# ---- answers that are not a tax list --------------------------------------------

def test_a_pair_with_no_tax_is_an_empty_list_not_an_error():
    # the live search page always carries its form, with a "Tax Code" label
    empty = tax_page() + '<label class="FNTFieldName"> Tax Code</label>'
    assert pp.parse_tax_list(empty) == []
    _z, res = run([], routes_text="AUH-BKK")
    assert kinds(res) == [(ts.NO_ROUTE_TAX, "AUH-BKK", "")]


def test_a_page_that_is_not_the_tax_screen_stops_with_a_reason():
    with pytest.raises(pp.LayoutChanged, match="not the tax screen"):
        pp.parse_tax_list("<html>Please sign in</html>")
    moved = tax_list([(1, "Fee", "AE", "AUH", "BKK", "75 AED")]).replace(
        "<th>Tax Code</th>", "<th>Code</th>")
    with pytest.raises(pp.LayoutChanged, match="column headings"):
        pp.parse_tax_list(moved)


def test_one_unreadable_pair_is_skipped_and_named():
    class OneOdd(FakeTaxScreen):
        def tax_search(self, form):
            if {form["RecCodeDestGeo"], form["RecCodeDestGeoLinked"]} == {
                    "5543", "6207"}:
                return "<html>odd</html>"
            return super().tax_search(form)
    z = OneOdd(AUH)
    res = ts.run_tax_setup_check(z, ts.TaxSetupSettings(
        routes_text="AUH-BKK AUH-CAN AUH-DAC", date_ddmmyyyy="03/11/2026"))
    assert ("AUH", "CAN") in res.unread and ("AUH", "BKK") not in res.unread
    assert any(n.startswith("AUH-CAN") and "skipped" in n for n in res.notes)
    assert (ts.NOT_READ, "AUH-CAN", "") in kinds(res)


def test_nothing_readable_at_all_stops_the_run():
    class Gone(FakeTaxScreen):
        def tax_search(self, form):
            return "<html>Please sign in</html>"
    with pytest.raises(pp.LayoutChanged, match="first 3 airport pairs"):
        ts.run_tax_setup_check(Gone(AUH), ts.TaxSetupSettings(
            routes_text="AUH-*", date_ddmmyyyy="03/11/2026"))
