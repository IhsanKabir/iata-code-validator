"""Agency visit reports.

Fixtures are synthetic: the real reports carry agency contacts and phone
numbers. Every case mirrors something the August/July 2026 files actually do.
"""
from datetime import date

import pytest
from openpyxl import Workbook, load_workbook

from src import visit_master as vm


# --------------------------------------------------------------------------
# the billing figure
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [
    ("BDT- 45433558/-", 45433558),      # the hyphen is punctuation, not a minus
    ("BDT 0/-", 0),                     # a real zero, kept as zero
    ("45,433,558", 45433558),
    ("3 lakh", 300_000),
    ("1.5 crore", 15_000_000),
    ("", None),
    (None, None),
])
def test_a_billing_figure_is_read_as_written(raw, expected):
    assert vm.parse_money(raw) == expected


@pytest.mark.parametrize("raw", [
    "Per month average 3-5 lakh",       # a range is a conversation
    "20-25 lakh",
    "3 to 5 lakh",
    "At present Bs tickets issued through other agency",
    "Not disclosed",
])
def test_a_figure_nobody_pinned_down_is_not_invented(raw):
    """Reading '3-5 lakh' as 3 would understate an agency by five orders."""
    assert vm.parse_money(raw) is None


def test_zero_and_blank_are_not_the_same_answer():
    assert vm.parse_money("BDT 0/-") == 0        # they told us: nothing
    assert vm.parse_money("") is None            # nobody asked


# --------------------------------------------------------------------------
# dates and zones
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [
    ("11/08/2026   (Tuesday)", date(2026, 8, 11)),
    ("2 Aug, Sunday", date(2026, 8, 2)),         # no year written at all
    ("03 Aug, Monday", date(2026, 8, 3)),
    ("17/08/2026  (Monday)", date(2026, 8, 17)),
    ("", None),
    ("no date here", None),
])
def test_visit_dates_however_they_are_written(raw, expected):
    assert vm.parse_visit_date(raw, month=8, year=2026) == expected


def test_a_real_date_cell_is_used_as_a_date():
    from datetime import datetime
    assert vm.parse_visit_date(datetime(2026, 8, 5, 9, 30)) == date(2026, 8, 5)


@pytest.mark.parametrize("raw, expected", [
    ("Zone #   1", "Zone 1"),
    ("Zone: 9", "Zone 9"),
    ("Zone 26", "Zone 26"),
    ("Zone #16", "Zone 16"),
    ("", ""),
    ("Motijheel", ""),
])
def test_zones_however_they_are_written(raw, expected):
    assert vm.parse_zone(raw) == expected


# --------------------------------------------------------------------------
# reading a report
# --------------------------------------------------------------------------
HEADER = ["Sl.", "Name of Agent", "Contact Person", "Designation", "Contact No",
          "Location", "Productivity / Monthly Sale", "Most Selling Route",
          "Remarks"]


def _block(ws, day_text, zone_text, rep, rows):
    ws.append(["Daily Agency & Corporate Visit"])
    ws.append(["", "", "", "", "Date & Day:", day_text, "", zone_text])
    ws.append(["", "", "", "", "Name:", rep])
    ws.append(["Daily Agency Sales Visit"])
    ws.append(HEADER)
    for i, r in enumerate(rows, start=1):
        ws.append([str(i)] + list(r))


def _report(path, sheets):
    wb = Workbook()
    wb.remove(wb.active)
    for name, blocks in sheets.items():
        ws = wb.create_sheet(name)
        for day_text, zone_text, rep, rows in blocks:
            _block(ws, day_text, zone_text, rep, rows)
    wb.save(path)


ROW_A = ["Alpha Travels", "Mr. A", "Owner", "01700000001", "Motijheel",
         "BDT- 1000000/-", "KSA, DOH", "fine"]
ROW_B = ["Beta Tours", "Ms. B", "Manager", "01700000002", "Motijheel",
         "Per month average 3-5 lakh", "KUL, SIN", ""]


def test_a_report_is_read_block_by_block(tmp_path):
    p = tmp_path / "visits.xlsx"
    _report(p, {"rep1": [
        ("11/08/2026   (Tuesday)", "Zone #   1", "ALEX ROY", [ROW_A, ROW_B]),
        ("12/08/2026   (Wednesday)", "Zone #   1", "ALEX ROY", [ROW_A]),
    ]})
    data = vm.parse_workbook(p, month=8, year=2026)
    assert len(data.visits) == 3
    assert data.blocks == 2
    assert {v.rep for v in data.visits} == {"ALEX ROY"}
    assert {v.zone for v in data.visits} == {"Zone 1"}
    assert sorted(data.days) == [date(2026, 8, 11), date(2026, 8, 12)]
    # the range figure is not read as a number, and that is recorded
    figs = [v.productivity for v in data.visits]
    assert figs.count(None) == 1
    assert any(i.kind == "no_productivity" for i in data.issues)


def test_the_columns_are_re_read_for_every_block(tmp_path):
    """One block says 'Productivity / Monthly Sale', the next says
    'Monthly Sale', and the columns move. Reading them once loses the rest."""
    p = tmp_path / "visits.xlsx"
    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("rep1")
    _block(ws, "11/08/2026", "Zone # 2", "ALEX ROY", [ROW_A])
    # a second block with a different column order and spelling
    ws.append(["Daily Agency & Corporate Visit"])
    ws.append(["", "", "", "", "Date & Day:", "12/08/2026", "", "Zone # 2"])
    ws.append(["", "", "", "", "Name:", "ALEX ROY"])
    ws.append(["Sl.", "Name of Agent", "Location", "Monthly Sale",
               "Contact Person", "Designation", "Contact No"])
    ws.append(["1", "Gamma Air", "Uttara", "BDT- 250000/-", "Mr. G", "Owner",
               "01700000003"])
    wb.save(p)

    data = vm.parse_workbook(p, month=8, year=2026)
    assert len(data.visits) == 2
    gamma = [v for v in data.visits if v.agency == "Gamma Air"][0]
    assert gamma.productivity == 250000
    assert gamma.location == "Uttara"
    assert gamma.contact == "Mr. G"


def test_a_date_from_another_month_keeps_its_day(tmp_path):
    """A block carrying a March date in an August report is a copied template.
    The day is what the rep meant; the month comes from the report."""
    p = tmp_path / "visits.xlsx"
    _report(p, {"rep1": [
        ("07/03/2026   (Saturday)", "Zone # 3", "ALEX ROY", [ROW_A])]})
    data = vm.parse_workbook(p, month=8, year=2026)
    assert data.visits[0].day == date(2026, 8, 7)
    assert any(i.kind == "date_outside_month" for i in data.issues)


def test_a_numbered_row_with_no_agency_is_skipped_and_reported(tmp_path):
    p = tmp_path / "visits.xlsx"
    _report(p, {"rep1": [("11/08/2026", "Zone # 1", "ALEX ROY", [
        ROW_A, ["", "Mr. X", "Owner", "01700000009", "Motijheel", "", "", ""]])]})
    data = vm.parse_workbook(p, month=8, year=2026)
    assert len(data.visits) == 1
    assert any(i.kind == "no_agency" for i in data.issues)


def test_an_empty_sheet_is_counted_not_parsed(tmp_path):
    p = tmp_path / "visits.xlsx"
    wb = Workbook()
    wb.remove(wb.active)
    wb.create_sheet("blank")
    ws = wb.create_sheet("rep1")
    _block(ws, "11/08/2026", "Zone # 1", "ALEX ROY", [ROW_A])
    wb.save(p)
    data = vm.parse_workbook(p, month=8, year=2026)
    assert data.empty_sheets == 1
    assert data.sheets == 1
    assert len(data.visits) == 1


# --------------------------------------------------------------------------
# the summary
# --------------------------------------------------------------------------
def test_revisiting_one_agency_is_not_seeing_two(tmp_path):
    p = tmp_path / "visits.xlsx"
    _report(p, {"rep1": [
        ("11/08/2026", "Zone # 1", "ALEX ROY", [ROW_A, ROW_A]),
        ("12/08/2026", "Zone # 1", "ALEX ROY", [ROW_A])]})
    data = vm.parse_workbook(p, month=8, year=2026)
    reps, _zones, agencies = vm.summarise(data)
    r = reps["ALEX ROY"]
    assert r.visits == 3
    assert len(r.agencies) == 1        # one agency, three visits
    assert r.repeats == 2
    assert len(agencies) == 1
    # keyed by identity, so the spelling does not decide the key
    assert next(iter(agencies.values()))["visits"] == 3


def test_a_missing_figure_never_becomes_a_zero_in_the_total(tmp_path):
    p = tmp_path / "visits.xlsx"
    _report(p, {"rep1": [("11/08/2026", "Zone # 1", "ALEX ROY",
                          [ROW_A, ROW_B])]})
    data = vm.parse_workbook(p, month=8, year=2026)
    reps, _z, _a = vm.summarise(data)
    r = reps["ALEX ROY"]
    assert r.productivity == 1_000_000      # only the figure that was written
    assert r.with_figure == 1
    assert r.figure_rate == pytest.approx(0.5)


# --------------------------------------------------------------------------
# end to end
# --------------------------------------------------------------------------
def test_the_master_sheet_is_one_sheet_with_every_section(tmp_path):
    folder = tmp_path / "reports"
    folder.mkdir()
    _report(folder / "aug.xlsx", {"rep1": [
        ("11/08/2026", "Zone # 1", "ALEX ROY", [ROW_A, ROW_B]),
        ("12/08/2026", "Zone # 1", "ALEX ROY", [ROW_A])],
        "rep2": [("11/08/2026", "Zone # 2", "SAM LEE", [ROW_B])]})
    out = tmp_path / "m.xlsx"
    res = vm.build_master(folder, out, month=8, year=2026)

    assert res.reps == 2
    assert res.visits == 4
    assert res.agencies == 2
    assert res.days == 2
    book = load_workbook(out)
    assert book.sheetnames == ["Visits"]
    text = "\n".join(str(c.value) for row in book["Visits"].iter_rows()
                     for c in row if c.value is not None)
    for section in ("AT A GLANCE", "SALES REP SCORECARD", "ZONE COVERAGE",
                    "AGENCIES BY BILLING SEEN", "WHAT THE AGENCIES SAY THEY SELL",
                    "DATA QUALITY"):
        assert section in text
    book.close()


def test_files_and_folders_can_be_mixed(tmp_path):
    folder = tmp_path / "reports"
    folder.mkdir()
    a = folder / "aug.xlsx"
    loose = tmp_path / "extra.xlsx"
    for p in (a, loose):
        _report(p, {"rep1": [("11/08/2026", "Zone # 1", "ALEX ROY", [ROW_A])]})
    found = vm.resolve_visit_inputs([folder, a, loose])
    assert [p.name for p in found] == ["aug.xlsx", "extra.xlsx"]


def test_an_empty_selection_is_an_error_not_an_empty_report(tmp_path):
    with pytest.raises(ValueError, match="No visit report"):
        vm.read_all(tmp_path)


# --------------------------------------------------------------------------
# identity, duplicates and routes
# --------------------------------------------------------------------------
@pytest.mark.parametrize("a, b", [
    ("AB Travel", "AB Travels"),
    ("ALIF TRAVELS", "Alif Travels"),
    ("Addcom Tour and Travel", "ADDCOM TOUR AND TRAVELS"),
    ("Tour Planners", "Tour Planners Limited"),
    ("A'Shahad Travels", "A'shahad Travels"),
])
def test_one_agency_written_two_ways_is_one_agency(a, b):
    assert vm.identity(a) == vm.identity(b)


def test_two_different_agencies_stay_different():
    assert vm.identity("Alpha Travels") != vm.identity("Beta Travels")
    assert vm.identity("Sky Way Victory") != vm.identity("Victory Travels")


def test_one_rep_written_three_ways_is_one_rep():
    """A single sheet carried all three of these, splitting the person's work
    three ways in a scorecard meant to compare people."""
    canon = vm.canonical_people([
        "Md Barru Ibna Azam Barno", "Md. Barru Ibna Azam",
        "Md. Barru Ibna Azam Barno"])
    assert len(set(canon.values())) == 1


def test_two_different_reps_on_one_sheet_stay_two():
    canon = vm.canonical_people(["Md. Mahmudul Hasan", "Yeachir Arafat"])
    assert len(set(canon.values())) == 2


def test_the_same_report_filed_twice_does_not_double_the_month(tmp_path):
    """A Downloads folder routinely holds 'report.xlsx' and 'report (1).xlsx'.
    Reading both turned 2,201 visits into 4,944."""
    a = tmp_path / "report.xlsx"
    b = tmp_path / "report (1).xlsx"
    for path in (a, b):
        _report(path, {"rep1": [
            ("11/08/2026", "Zone # 1", "ALEX ROY", [ROW_A, ROW_B])]})
    once = vm.read_all([a], month=8, year=2026)
    twice = vm.read_all([a, b], month=8, year=2026)
    assert len(once.visits) == 2
    assert len(twice.visits) == 2          # not 4
    assert twice.duplicates == 2


def test_a_genuinely_new_visit_in_a_second_file_is_kept(tmp_path):
    a = tmp_path / "report.xlsx"
    b = tmp_path / "report (1).xlsx"
    _report(a, {"rep1": [("11/08/2026", "Zone # 1", "ALEX ROY", [ROW_A])]})
    _report(b, {"rep1": [("12/08/2026", "Zone # 1", "ALEX ROY", [ROW_A])]})
    both = vm.read_all([a, b], month=8, year=2026)
    assert len(both.visits) == 2           # same agency, different days
    assert both.duplicates == 0


@pytest.mark.parametrize("written, expected", [
    ("SPD-DAC-SPD", ["SPD", "DAC", "SPD"]),
    ("KUL, SIN, BKK", ["KUL", "SIN", "BKK"]),
    ("MCT/BD/MCT", ["MCT", "MCT"]),        # BD is not an airport code
    ("Dxb, Auh", ["DXB", "AUH"]),
    ("Middle East all sector.", ["Middle East"]),
])
def test_a_route_is_read_as_sectors_not_as_one_mangled_word(written, expected):
    """Not splitting on the hyphen turned SPD-DAC-SPD into 'Spddacspd', 201
    times over."""
    data = vm.VisitData(visits=[vm.Visit(rep="R", sheet="s", day=None, zone="",
                                         agency="A", routes=written)])
    got = vm.route_demand(data)
    for token in expected:
        assert got[token] >= 1
    assert "Spddacspd" not in got


def test_two_reps_giving_one_agency_different_figures_is_reported(tmp_path):
    """68 agencies were given two figures, up to seven times apart. Showing
    only the larger would hide that they disagree."""
    p = tmp_path / "visits.xlsx"
    _report(p, {"rep1": [("11/08/2026", "Zone # 1", "ALEX ROY", [
        ["Alpha Travels", "Mr. A", "Owner", "01700000001", "Motijheel",
         "BDT- 1000000/-", "KSA", ""]])],
        "rep2": [("11/08/2026", "Zone # 2", "SAM LEE", [
            ["Alpha Travels", "Mr. A", "Owner", "01700000009", "Motijheel",
             "BDT- 7000000/-", "KSA", ""]])]})
    data = vm.read_all([p], month=8, year=2026)
    _reps, _zones, agencies = vm.summarise(data)
    a = next(iter(agencies.values()))
    assert a["disputed"] is True
    assert a["figures"] == {1_000_000, 7_000_000}


# --------------------------------------------------------------------------
# did the visits change anything?
# --------------------------------------------------------------------------
def _sales(rows):
    """(customer, day, amount) as the warehouse hands them over."""
    return [(c, d, a) for c, d, a in rows]


def test_the_lift_is_measured_against_agencies_nobody_visited(tmp_path):
    """Visited agencies fell 5.8% in August, which reads as a failure until you
    see that agencies nobody visited fell 8.1% over the same days."""
    p = tmp_path / "visits.xlsx"
    _report(p, {"rep1": [("05/08/2026", "Zone # 1", "ALEX ROY", [ROW_A])]})
    data = vm.read_all([p], month=8, year=2026)
    rows = _sales([
        # the visited agency: 100 -> 90, down a tenth
        ("Alpha Travels", date(2026, 7, 20), 100.0),
        ("Alpha Travels", date(2026, 8, 10), 90.0),
        # everyone else: 100 -> 50, down by half
        ("Other Agency", date(2026, 7, 20), 100.0),
        ("Other Agency", date(2026, 8, 10), 50.0),
    ])
    res = vm.measure_impact(data, rows, month=8, year=2026)
    assert res.visited_change == pytest.approx(-0.10)
    assert res.control_change == pytest.approx(-0.50)
    assert res.lift == pytest.approx(0.40)      # beat the market by 40 points


def test_an_agency_with_no_sales_is_reported_not_counted_as_zero(tmp_path):
    p = tmp_path / "visits.xlsx"
    _report(p, {"rep1": [("05/08/2026", "Zone # 1", "ALEX ROY",
                          [ROW_A, ROW_B])]})
    data = vm.read_all([p], month=8, year=2026)
    rows = _sales([("Alpha Travels", date(2026, 8, 10), 500.0)])
    res = vm.measure_impact(data, rows, month=8, year=2026)
    beta = [a for a in res.agencies if a.name == "Beta Tours"][0]
    assert beta.matched is False
    assert beta.verdict == "no sales found"
    # and it does not drag the visited total down as if it had bought nothing
    assert res.visited_after == pytest.approx(500.0)


@pytest.mark.parametrize("before, after, verdict", [
    (100.0, 200.0, "grew"),
    (100.0, 50.0, "fell"),
    (100.0, 101.0, "flat"),
    (0.0, 100.0, "started buying"),
    (100.0, 0.0, "stopped buying"),
])
def test_each_agency_gets_the_right_verdict(tmp_path, before, after, verdict):
    p = tmp_path / "visits.xlsx"
    _report(p, {"rep1": [("05/08/2026", "Zone # 1", "ALEX ROY", [ROW_A])]})
    data = vm.read_all([p], month=8, year=2026)
    rows = []
    if before:
        rows.append(("Alpha Travels", date(2026, 7, 20), before))
    if after:
        rows.append(("Alpha Travels", date(2026, 8, 10), after))
    if not rows:                       # matched but silent both sides
        rows = [("Alpha Travels", date(2026, 7, 20), 0.0)]
    res = vm.measure_impact(data, rows, month=8, year=2026)
    assert res.agencies[0].verdict == verdict


def test_early_and_late_visits_are_measured_apart(tmp_path):
    """A visit on the 25th cannot have caused sales on the 3rd, so the two are
    reported separately -- it is the only internal control available."""
    p = tmp_path / "visits.xlsx"
    _report(p, {"rep1": [
        ("03/08/2026", "Zone # 1", "ALEX ROY", [ROW_A]),
        ("25/08/2026", "Zone # 1", "ALEX ROY", [ROW_B])]})
    data = vm.read_all([p], month=8, year=2026)
    rows = _sales([
        ("Alpha Travels", date(2026, 7, 20), 100.0),
        ("Alpha Travels", date(2026, 8, 10), 200.0),
        ("Beta Tours", date(2026, 7, 20), 100.0),
        ("Beta Tours", date(2026, 8, 10), 100.0),
        ("Other", date(2026, 7, 20), 100.0),
        ("Other", date(2026, 8, 10), 100.0),
    ])
    res = vm.measure_impact(data, rows, month=8, year=2026)
    assert res.early_before == pytest.approx(100.0)
    assert res.early_after == pytest.approx(200.0)
    assert res.late_before == pytest.approx(100.0)
    assert res.late_after == pytest.approx(100.0)
    assert res.early_lift > res.late_lift


def test_the_biggest_buyer_nobody_visited_is_named(tmp_path):
    p = tmp_path / "visits.xlsx"
    _report(p, {"rep1": [("05/08/2026", "Zone # 1", "ALEX ROY", [ROW_A])]})
    data = vm.read_all([p], month=8, year=2026)
    rows = _sales([
        ("Alpha Travels", date(2026, 8, 10), 100.0),
        ("Takeoff Travels", date(2026, 8, 10), 900.0),
    ])
    res = vm.measure_impact(data, rows, month=8, year=2026)
    assert res.unvisited_top[0][0] == "Takeoff Travels"
    assert res.unvisited_top[0][1] == pytest.approx(900.0)


def test_sales_outside_both_windows_are_ignored(tmp_path):
    p = tmp_path / "visits.xlsx"
    _report(p, {"rep1": [("05/08/2026", "Zone # 1", "ALEX ROY", [ROW_A])]})
    data = vm.read_all([p], month=8, year=2026)
    rows = _sales([
        ("Alpha Travels", date(2026, 5, 1), 9999.0),     # long before
        ("Alpha Travels", date(2026, 7, 20), 100.0),
        ("Alpha Travels", date(2026, 8, 10), 100.0),
    ])
    res = vm.measure_impact(data, rows, month=8, year=2026)
    assert res.visited_before == pytest.approx(100.0)
    assert res.visited_after == pytest.approx(100.0)


# --------------------------------------------------------------------------
# identity must merge spellings without merging businesses
# --------------------------------------------------------------------------
def test_three_businesses_sharing_a_first_word_stay_three():
    """Deleting the trade words merged SHEBA AIR SERVICE, SHEBA AIR TRAVELS and
    SHEBA TRAVELS & TOURS into one agency, and 406 keys held three or more
    different customers."""
    names = ["SHEBA AIR SERVICE", "SHEBA AIR TRAVELS", "SHEBA TRAVELS & TOURS"]
    assert len({vm.identity(n) for n in names}) == 3
    ss = ["S S Holidays", "S.S Enterprise", "S.S International"]
    assert len({vm.identity(n) for n in ss}) == 3


@pytest.mark.parametrize("a, b", [
    ("AB Travel", "AB Travels"),
    ("ALIF TRAVELS", "Alif Travels"),
    ("Sharetrip limited", "Share Trip Limited"),
    ("CARNIVAL AIR TICKETING LTD.", "Carnival Air Ticketing Ltd. (IATA)"),
    ("Real Journey Tours & Travels", "REAL JOURNEY TOURS AND TRAVELS"),
    ("Rain Tours And Travels", "M/S Rain Tours And Travels"),
])
def test_one_agency_spelled_two_ways_still_merges(a, b):
    assert vm.identity(a) == vm.identity(b)


# --------------------------------------------------------------------------
# the lift is a range, and the control has to be like for like
# --------------------------------------------------------------------------
def test_the_control_is_not_flattered_by_agencies_that_had_no_before(tmp_path):
    """12,743 control agencies had no July sales at all and could only go up,
    against 52 on the visited side. Left in, they drag the comparison."""
    p = tmp_path / "visits.xlsx"
    _report(p, {"rep1": [("05/08/2026", "Zone # 1", "ALEX ROY", [ROW_A])]})
    data = vm.read_all([p], month=8, year=2026)
    rows = [
        ("Alpha Travels", date(2026, 7, 20), 100.0),
        ("Alpha Travels", date(2026, 8, 10), 90.0),      # visited: -10%
        ("Trading Other", date(2026, 7, 20), 100.0),
        ("Trading Other", date(2026, 8, 10), 50.0),      # control: -50%
        ("Brand New Agency", date(2026, 8, 10), 400.0),  # no before at all
    ]
    res = vm.measure_impact(data, rows, month=8, year=2026)
    # the wide control is flattered by the newcomer
    assert res.control_change > -0.5
    # the like-for-like basis is not
    assert vm.ImpactResult._rate(*res.trading_control) == pytest.approx(-0.5)
    assert res.trading_lift == pytest.approx(0.4)
    assert res.started_control == 1


def test_every_way_of_measuring_it_is_reported(tmp_path):
    p = tmp_path / "visits.xlsx"
    _report(p, {"rep1": [("05/08/2026", "Zone # 1", "ALEX ROY", [ROW_A])]})
    data = vm.read_all([p], month=8, year=2026)
    rows = [
        ("Alpha Travels", date(2026, 7, 20), 100.0),
        ("Alpha Travels", date(2026, 8, 10), 90.0),
        ("Trading Other", date(2026, 7, 20), 100.0),
        ("Trading Other", date(2026, 8, 10), 50.0),
    ]
    res = vm.measure_impact(data, rows, month=8, year=2026)
    lo, hi = res.lift_range
    assert lo is not None and hi is not None
    assert lo <= res.trading_lift <= hi
    # an exact-match-only figure exists even when nothing was matched loosely
    assert res.exact_lift is not None


# --- reading the month off the reports -------------------------------------

def test_the_month_is_read_from_the_dates_the_reps_wrote(tmp_path):
    """The counter tab has done this since it was asked to; this one defaulted
    to today's month, and on 3 September read the August reports as
    September."""
    p = tmp_path / "visits.xlsx"
    _report(p, {"S1": [("12/08/2026", "Zone 7", "REP ONE", [ROW_A]),
                       ("13/08/2026", "Zone 7", "REP ONE", [ROW_B])]})
    month, year, votes, agree = vm.detect_period([p])
    assert (month, year) == (8, 2026)
    assert agree == 1.0


def test_one_rep_writing_a_stale_date_does_not_decide_the_month(tmp_path):
    p = tmp_path / "visits.xlsx"
    blocks = [(f"{d:02d}/08/2026", "Zone 7", "REP ONE", [ROW_A])
              for d in range(1, 6)]
    blocks.append(("28/07/2026", "Zone 7", "REP TWO", [ROW_B]))
    _report(p, {"S1": blocks})
    month, year, _votes, agree = vm.detect_period([p])
    assert (month, year) == (8, 2026)
    assert agree < 1.0


def test_reports_with_no_readable_date_say_so_rather_than_guessing(tmp_path):
    p = tmp_path / "visits.xlsx"
    _report(p, {"S1": [("", "Zone 7", "REP ONE", [ROW_A])]})
    month, year, votes, agree = vm.detect_period([p])
    assert month is None and year is None and not votes


def test_the_wrong_month_is_called_out_where_nobody_can_miss_it(tmp_path):
    """Relabelling 2,519 August visits as September printed a window nobody
    worked. The number was in a data-quality row; it belongs at the top."""
    src = tmp_path / "visits.xlsx"
    _report(src, {"S1": [(f"{d:02d}/08/2026", "Zone 7", "REP ONE",
                          [["Agency %d" % d] + ROW_A[1:]])
                         for d in range(1, 6)]})
    out = tmp_path / "master.xlsx"
    vm.build_master([src], out, month=9, year=2026)      # the wrong month
    ws = load_workbook(out)["Visits"]
    head = " ".join(str(c.value) for row in ws.iter_rows(min_row=1, max_row=3)
                    for c in row if c.value)
    assert "CHECK THE MONTH" in head
    assert "Aug 2026" in head              # names the month the dates say
    assert "100%" in head


def test_the_right_month_carries_no_warning(tmp_path):
    src = tmp_path / "visits.xlsx"
    _report(src, {"S1": [(f"{d:02d}/08/2026", "Zone 7", "REP ONE",
                          [["Agency %d" % d] + ROW_A[1:]])
                         for d in range(1, 6)]})
    out = tmp_path / "master.xlsx"
    vm.build_master([src], out, month=8, year=2026)
    ws = load_workbook(out)["Visits"]
    head = " ".join(str(c.value) for row in ws.iter_rows(min_row=1, max_row=3)
                    for c in row if c.value)
    assert "CHECK THE MONTH" not in head


def test_the_share_of_moved_dates_cannot_exceed_everything(tmp_path):
    """Issues are logged before duplicates are dropped; dividing one by the
    other reported 104% of the dates as wrong."""
    src = tmp_path / "visits.xlsx"
    block = ("12/08/2026", "Zone 7", "REP ONE", [ROW_A])
    _report(src, {"S1": [block, block, block]})       # two are duplicates
    data = vm.read_all([src], month=9, year=2026)
    dated = [v for v in data.visits if v.day]
    assert sum(1 for v in dated if v.date_moved) <= len(dated)
