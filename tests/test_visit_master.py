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
