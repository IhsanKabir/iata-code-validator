"""Counter reports vs the sales report.

Fixtures are synthetic — a real sales report carries customer names and PNRs.
Each case mirrors something the August 2026 data actually did.
"""
from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook

from src import counter_master as cm
from src import counter_reconcile as cr

SALES_HEADERS = ["Date", "Transaction", "PNR Zenith", "Record Locator", "Heading",
                 "Point of sales", "Customer", "Balance (base currency)",
                 "Sale agent"]


def _sales_report(path, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Airline sales statements"
    ws.append(SALES_HEADERS)
    for r in rows:
        ws.append(r)
    wb.save(path)


def _line(day, txn, locator, pos, amount, heading="", agent="AGENT ONE"):
    return [date(2026, 8, day), txn, "17146250", locator, heading, pos,
            "SOME CUSTOMER", amount, agent]


# --------------------------------------------------------------------------
# reading the sales report
# --------------------------------------------------------------------------
def test_sales_lines_are_grouped_per_pnr_per_day(tmp_path):
    p = tmp_path / "sales.xlsx"
    _sales_report(p, [
        _line(16, "Ticket payment", "0A1111", "DAC-07 Baridhara", 10000),
        _line(16, "Commission", "0A1111", "DAC-07 Baridhara", -500),
    ])
    data = cr.read_sales_report(p)
    assert len(data.lines) == 1
    assert data.lines[0].block == "ISSUE"
    assert data.lines[0].amount == pytest.approx(9500)   # net of commission
    assert data.first_day == date(2026, 8, 16)


def test_a_penalty_follows_whatever_its_pnr_was_doing(tmp_path):
    """A Penalty line alone says nothing — the other lines on the PNR decide."""
    p = tmp_path / "sales.xlsx"
    _sales_report(p, [
        _line(16, "Refund", "0A2222", "DAC-07 Baridhara", -8000),
        _line(16, "Penalty", "0A2222", "DAC-07 Baridhara", 1000),
        _line(17, "Ticket payment", "0A3333", "DAC-07 Baridhara", 5000),
        _line(17, "Penalty", "0A3333", "DAC-07 Baridhara", 500),
    ])
    data = cr.read_sales_report(p)
    by = {(ln.locator, ln.block): ln.amount for ln in data.lines}
    assert by[("0A2222", "REFUND")] == pytest.approx(7000)   # 8000 less the 1000
    assert by[("0A3333", "ISSUE")] == pytest.approx(5500)    # 5000 plus the 1000


def test_voided_tickets_are_not_sales(tmp_path):
    p = tmp_path / "sales.xlsx"
    _sales_report(p, [
        _line(16, "Ticket void", "0A4444", "DAC-07 Baridhara", 9000),
    ])
    data = cr.read_sales_report(p)
    assert data.lines == []
    assert data.voided == 1


def test_a_wrong_workbook_is_rejected_with_the_missing_columns(tmp_path):
    p = tmp_path / "not-sales.xlsx"
    wb = Workbook()
    wb.active.append(["Name", "Amount"])
    wb.save(p)
    with pytest.raises(ValueError, match="Record Locator"):
        cr.read_sales_report(p)


# --------------------------------------------------------------------------
# counter <-> point of sale
# --------------------------------------------------------------------------
@pytest.mark.parametrize("counter, pos", [
    ("Baridhara", "DAC-07 Baridhara"),
    ("Uttara", "DAC-06 Uttara"),
    ("Motijhreel", "DAC-04 Motijheel"),        # counter spells it differently
    ("Dhamnondi", "DAC-05 Dhanmondi"),
    ("Banashree", "DAC-08 Banoshree"),
    ("Rongpur", "SPD-3 Rangpur City"),         # different spelling AND prefix
    ("RJH", "RJH-2  Rajshahi City"),           # two spaces in the source
    ("DOH", "INT Doha (Qatar)"),               # station code -> city name
    ("KUL", "INT Kuala Lumpur (Malaysia)"),
    ("Cox Bazar", "CXB-1 Cox's Bazar"),
])
def test_counters_map_to_their_point_of_sale(counter, pos):
    all_pos = ["DAC-07 Baridhara", "DAC-06 Uttara", "DAC-04 Motijheel",
               "DAC-05 Dhanmondi", "DAC-08 Banoshree", "SPD-3 Rangpur City",
               "RJH-2  Rajshahi City", "INT Doha (Qatar)",
               "INT Kuala Lumpur (Malaysia)", "CXB-1 Cox's Bazar",
               "SPD-2 Saidpur City", "EXTRANET AGENC.", "WEB"]
    mapping, _s, _uc, _up, _amb = cr.suggest_mapping([counter], all_pos)
    assert mapping.get(counter) == pos


def test_a_city_counter_does_not_bind_to_the_airport_desk():
    mapping, _s, _uc, _up, _amb = cr.suggest_mapping(
        ["ZYL"], ["ZYL-1 Airport (Sylhet)", "ZYL-2 Sylhet City"])
    assert mapping["ZYL"] == "ZYL-2 Sylhet City"


def test_gds_and_web_channels_are_never_counters():
    for pos in ("EXTRANET AGENC.", "Galileo 1G 1G", "WEB", "MOBIAPP",
                "BO-1 Central Reservation", "Sabre 1S 1S"):
        assert cr.is_counter_pos(pos) is False
    assert cr.is_counter_pos("DAC-07 Baridhara") is True


def test_points_of_sale_with_no_counter_are_reported():
    _m, _s, _uc, unmatched, _amb = cr.suggest_mapping(
        ["Baridhara"], ["DAC-07 Baridhara", "DAC-01 Airport (Dhaka)", "WEB"])
    assert unmatched == ["DAC-01 Airport (Dhaka)"]


# --------------------------------------------------------------------------
# the comparison
# --------------------------------------------------------------------------
def _rows(*specs):
    return [cm.SaleRow(counter=c, day=d, block=b, emp_id="USBA-90001",
                       emp_name="ALEX ROY", pnr=p, amount=a, currency=cur)
            for c, d, b, p, a, cur in specs]


def _data(tmp_path, lines):
    p = tmp_path / "sales.xlsx"
    _sales_report(p, lines)
    return cr.read_sales_report(p)


MAP = {"Baridhara": "DAC-07 Baridhara"}


def test_a_sale_the_counter_never_wrote_down_is_unreported(tmp_path):
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "DAC-07 Baridhara", 10000)])
    res = cr.reconcile(data, [], MAP)
    assert len(res.of(cr.UNREPORTED)) == 1
    assert res.unreported_amount == pytest.approx(10000)


def test_a_sale_logged_on_another_day_is_shifted_not_missing(tmp_path):
    """The counters close the day late; that is not a missing sale."""
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "DAC-07 Baridhara", 10000)])
    rows = _rows(("Baridhara", 17, "ISSUE", "0A1111", 10000, "BDT"))
    res = cr.reconcile(data, rows, MAP)
    assert res.of(cr.UNREPORTED) == []
    shifted = res.of(cr.DATE_SHIFTED)
    assert len(shifted) == 1
    assert "day 17" in shifted[0].note
    assert res.unreported_amount == 0


def test_a_matching_sale_is_clean_and_a_differing_amount_is_flagged(tmp_path):
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "DAC-07 Baridhara", 10000),
        _line(16, "Ticket payment", "0A2222", "DAC-07 Baridhara", 20000)])
    rows = _rows(("Baridhara", 16, "ISSUE", "0A1111", 10000, "BDT"),
                 ("Baridhara", 16, "ISSUE", "0A2222", 15000, "BDT"))
    res = cr.reconcile(data, rows, MAP)
    assert res.of(cr.UNREPORTED) == []
    flagged = [f for f in res.of(cr.MATCHED) if f.note]
    assert len(flagged) == 1
    assert flagged[0].locator == "0A2222"
    assert flagged[0].gap == pytest.approx(5000)


def test_something_the_counter_invented_is_not_in_system(tmp_path):
    # the export must span day 17, or day 17 is simply outside what it can judge
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "DAC-07 Baridhara", 10000),
        _line(17, "Ticket payment", "0A2222", "DAC-07 Baridhara", 3000)])
    rows = _rows(("Baridhara", 16, "ISSUE", "0A1111", 10000, "BDT"),
                 ("Baridhara", 17, "ISSUE", "0A2222", 3000, "BDT"),
                 ("Baridhara", 17, "ISSUE", "0A9999", 7000, "BDT"))
    res = cr.reconcile(data, rows, MAP)
    ghosts = res.of(cr.NOT_IN_SYSTEM)
    assert [g.locator for g in ghosts] == ["0A9999"]


def test_only_the_days_the_sales_report_covers_are_judged(tmp_path):
    """A one-week export must not report the other three weeks as invented."""
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "DAC-07 Baridhara", 10000)])
    rows = _rows(("Baridhara", 16, "ISSUE", "0A1111", 10000, "BDT"),
                 ("Baridhara", 2, "ISSUE", "0A7777", 5000, "BDT"),
                 ("Baridhara", 28, "ISSUE", "0A8888", 5000, "BDT"))
    res = cr.reconcile(data, rows, MAP)
    assert res.of(cr.NOT_IN_SYSTEM) == []      # day 2 and 28 are out of window
    assert res.of(cr.UNREPORTED) == []


def test_a_block_logged_as_the_wrong_type_is_its_own_category(tmp_path):
    data = _data(tmp_path, [
        _line(16, "Refund", "0A1111", "DAC-07 Baridhara", -9000)])
    rows = _rows(("Baridhara", 16, "ISSUE", "0A1111", 9000, "BDT"))
    res = cr.reconcile(data, rows, MAP)
    mism = res.of(cr.BLOCK_MISMATCH)
    assert len(mism) == 1
    assert "ISSUE" in mism[0].note
    assert res.of(cr.UNREPORTED) == []


def test_local_currency_counters_are_compared_on_counts_not_money(tmp_path):
    """A CNY sheet against a BDT export is not a shortfall, it is two measures."""
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "INT Guangzhou (China)", 556266)])
    res = cr.reconcile(data, [], {"CAN": "INT Guangzhou (China)"},
                       currency_by_counter={"CAN": "CNY"})
    assert res.non_comparable == ["CAN"]
    assert len(res.of(cr.UNREPORTED)) == 1        # the COUNT still counts
    assert res.unreported_amount == 0             # the money does not
    assert res.comparable("CAN") is False


def test_unreported_sales_are_attributed_to_the_system_agent(tmp_path):
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "DAC-07 Baridhara", 10000,
              agent="R KRISHNO"),
        _line(17, "Ticket payment", "0A2222", "DAC-07 Baridhara", 5000,
              agent="R KRISHNO")])
    res = cr.reconcile(data, [], MAP)
    assert res.per_agent["R KRISHNO"] == {"n": 2, "amt": 15000}


def test_an_unmapped_counter_is_skipped_rather_than_judged(tmp_path):
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "DAC-01 Airport (Dhaka)", 10000)])
    res = cr.reconcile(data, [], MAP)
    assert res.of(cr.UNREPORTED) == []
    assert ("DAC-01 Airport (Dhaka)", 1) in res.unmapped_pos


def test_an_empty_sales_report_judges_nothing(tmp_path):
    p = tmp_path / "sales.xlsx"
    _sales_report(p, [])
    data = cr.read_sales_report(p)
    res = cr.reconcile(data, _rows(
        ("Baridhara", 16, "ISSUE", "0A1111", 10000, "BDT")), MAP)
    assert res.findings == []
    assert res.first_day is None


# --------------------------------------------------------------------------
# omitted vs never filed, duplicates, currency sanity
# --------------------------------------------------------------------------
def test_a_sale_on_a_day_with_no_sheet_is_not_the_same_as_an_omission(tmp_path):
    """Two different failures. Blending them overstates per-sale omission."""
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "DAC-07 Baridhara", 10000),
        _line(17, "Ticket payment", "0A2222", "DAC-07 Baridhara", 20000)])
    # the counter filed a sheet on the 16th only, and left this sale off it
    rows = _rows(("Baridhara", 16, "ISSUE", "0A3333", 500, "BDT"))
    res = cr.reconcile(data, rows, MAP, filed_days={"Baridhara": {16}})

    omitted = res.omitted
    unfiled = res.unfiled
    assert [f.locator for f in omitted] == ["0A1111"]
    assert [f.locator for f in unfiled] == ["0A2222"]
    assert res.omitted_amount == pytest.approx(10000)
    assert res.unreported_amount == pytest.approx(30000)   # both kinds together


def test_without_filed_days_everything_counts_as_omitted(tmp_path):
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "DAC-07 Baridhara", 10000)])
    res = cr.reconcile(data, [], MAP)
    assert len(res.omitted) == 1
    assert res.unfiled == []


def test_multi_passenger_rows_are_summed_not_treated_as_extra_sales(tmp_path):
    """One PNR is one system line but several rows on the counter sheet."""
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "DAC-07 Baridhara", 30000)])
    rows = _rows(("Baridhara", 16, "ISSUE", "0A1111", 10000, "BDT"),
                 ("Baridhara", 16, "ISSUE", "0A1111", 10000, "BDT"),
                 ("Baridhara", 16, "ISSUE", "0A1111", 10000, "BDT"))
    res = cr.reconcile(data, rows, MAP, filed_days={"Baridhara": {16}})
    assert res.of(cr.NOT_IN_SYSTEM) == []      # the other two are not ghosts
    assert res.of(cr.UNREPORTED) == []
    assert [f for f in res.of(cr.MATCHED) if f.note] == []
    assert res.clean_matches == 1


def test_a_counter_writing_another_currency_is_caught_by_its_own_numbers(tmp_path):
    """DXB labels its column BDT but writes AED, so its sales look 58x smaller.
    The label cannot be trusted; the matched pairs give it away."""
    lines, rows = [], []
    for i, amt in enumerate([58000, 116000, 174000, 232000], start=1):
        loc = f"0A111{i}"
        lines.append(_line(16, "Ticket payment", loc, "INT Dubai City (Dubai)",
                           amt))
        rows.append(("DXB", 16, "ISSUE", loc, amt / 58, "BDT"))
    data = _data(tmp_path, lines)
    res = cr.reconcile(data, _rows(*rows), {"DXB": "INT Dubai City (Dubai)"},
                       currency_by_counter={"DXB": "BDT"},
                       filed_days={"DXB": {16}})
    assert "DXB" in res.currency_suspect
    assert res.currency_suspect["DXB"] == pytest.approx(58, rel=0.05)
    assert res.comparable("DXB") is False
    assert res.unreported_amount == 0          # no fake shortfall reported


def test_a_genuine_shortfall_is_not_mistaken_for_a_currency_problem(tmp_path):
    """Same-currency counters must stay comparable even when they under-report."""
    lines, rows = [], []
    for i in range(1, 5):
        loc = f"0A222{i}"
        lines.append(_line(16, "Ticket payment", loc, "DAC-07 Baridhara", 10000))
        rows.append(("Baridhara", 16, "ISSUE", loc, 10000, "BDT"))
    lines.append(_line(16, "Ticket payment", "0A9999", "DAC-07 Baridhara", 50000))
    data = _data(tmp_path, lines)
    res = cr.reconcile(data, _rows(*rows), MAP, filed_days={"Baridhara": {16}})
    assert res.currency_suspect == {}
    assert res.comparable("Baridhara") is True
    assert res.omitted_amount == pytest.approx(50000)


def test_clean_matches_are_counted(tmp_path):
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "DAC-07 Baridhara", 10000),
        _line(16, "Ticket payment", "0A2222", "DAC-07 Baridhara", 20000)])
    rows = _rows(("Baridhara", 16, "ISSUE", "0A1111", 10000, "BDT"),
                 ("Baridhara", 16, "ISSUE", "0A2222", 15000, "BDT"))
    res = cr.reconcile(data, rows, MAP, filed_days={"Baridhara": {16}})
    assert res.clean_matches == 1              # one clean, one amount mismatch


def test_a_second_desk_for_the_same_place_is_reported_as_ambiguous():
    _m, _s, _uc, _up, amb = cr.suggest_mapping(
        ["Uttara"], ["DAC-06 Uttara", "DAC-17 Uttara USBA-Office"])
    assert amb["Uttara"] == ["DAC-17 Uttara USBA-Office"]


# --------------------------------------------------------------------------
# finding the local sales data
# --------------------------------------------------------------------------
def test_no_warehouse_is_reported_as_absent_not_guessed(tmp_path):
    assert cr.find_sales_warehouse(extra_roots=[tmp_path / "nothing"],
                                   search_defaults=False) is None


def test_a_gold_file_is_found(tmp_path):
    gold = tmp_path / "gold"
    gold.mkdir()
    (gold / "sales_bi.parquet").write_bytes(b"")     # presence is what is probed
    src = cr.find_sales_warehouse(extra_roots=[tmp_path],
                                  search_defaults=False)
    assert src is not None
    assert src.kind == "gold"
    assert src.path.name == "sales_bi.parquet"


def test_a_partitioned_curated_set_is_found_when_there_is_no_gold(tmp_path):
    part = tmp_path / "curated" / "sales" / "year=2026"
    part.mkdir(parents=True)
    (part / "part-0.parquet").write_bytes(b"")
    src = cr.find_sales_warehouse(extra_roots=[tmp_path],
                                  search_defaults=False)
    assert src is not None
    assert src.kind == "curated"


def test_an_empty_curated_folder_is_not_a_warehouse(tmp_path):
    (tmp_path / "curated" / "sales").mkdir(parents=True)
    assert cr.find_sales_warehouse(extra_roots=[tmp_path],
                                  search_defaults=False) is None


@pytest.mark.parametrize("month, year, expected", [
    (8, 2026, True),
    (5, 2025, True),
    (4, 2025, False),      # before coverage starts
    (1, 2027, False),      # after it ends
])
def test_a_source_knows_whether_it_covers_the_month(month, year, expected):
    src = cr.WarehouseSource(path=Path("x"), kind="gold",
                             first_day=date(2025, 5, 1),
                             last_day=date(2026, 8, 31), rows=10)
    assert src.covers(month, year) is expected


def test_coverage_is_assumed_when_it_could_not_be_read():
    """An unreadable file is still found; the month check must not block on it."""
    src = cr.WarehouseSource(path=Path("x"), kind="gold")
    assert src.covers(1, 1999) is True
    assert "coverage unknown" in src.label
