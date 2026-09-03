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


def test_a_local_currency_sheet_is_not_compared_but_its_gaps_still_count(tmp_path):
    """A CNY sheet against a BDT export is two measures, so what the COUNTER
    wrote is not compared. What is MISSING has no counter-side figure at all --
    it is the system's own, in base currency -- so it counts in full."""
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "INT Guangzhou (China)", 556266)])
    res = cr.reconcile(data, [], {"CAN": "INT Guangzhou (China)"},
                       currency_by_counter={"CAN": "CNY"})
    assert res.non_comparable == ["CAN"]
    assert res.comparable("CAN") is False
    assert len(res.of(cr.UNREPORTED)) == 1
    assert res.unreported_amount == pytest.approx(556266)


def test_unreported_sales_are_attributed_to_the_system_agent(tmp_path):
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "DAC-07 Baridhara", 10000,
              agent="R KRISHNO"),
        _line(17, "Ticket payment", "0A2222", "DAC-07 Baridhara", 5000,
              agent="R KRISHNO")])
    res = cr.reconcile(data, [], MAP)
    assert res.per_agent["R KRISHNO"] == {"n": 2, "amt": 15000}


def test_an_unmapped_counter_is_reported_as_missing_not_skipped(tmp_path):
    """It used to be left out of the comparison entirely, which let a counter
    that sent nothing look better than one that sent an imperfect report."""
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "DAC-01 Airport (Dhaka)", 10000)])
    res = cr.reconcile(data, [], MAP)
    missing = res.of(cr.UNREPORTED)
    assert [f.counter for f in missing] == ["DAC-01 Airport (Dhaka)"]
    assert missing[0].submitted is False
    assert "no report submitted" in missing[0].note
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


def test_a_path_with_an_apostrophe_does_not_break_the_query(tmp_path):
    """C:/Users/O'Brien/... is an ordinary Windows path and ended the SQL string."""
    src = cr.WarehouseSource(path=Path("C:/Users/O'Brien/gold/sales_bi.parquet"),
                             kind="gold")
    with pytest.raises(Exception) as exc:
        cr.read_sales_from_warehouse(src, month=8, year=2026)
    # missing file is the right complaint; a parser error would mean the quote
    # escaped the string literal
    assert "syntax error" not in str(exc.value).lower()
    assert "no files found" in str(exc.value).lower()


def test_a_counter_row_at_the_month_edge_is_flagged_not_called_a_phantom(tmp_path):
    """The system side is one month, so a ticket issued on 31 Jul and logged by
    the counter on 1 Aug cannot match. That is not the same as an invention."""
    data = _data(tmp_path, [
        _line(1, "Ticket payment", "0A1111", "DAC-07 Baridhara", 1000),
        _line(31, "Ticket payment", "0A2222", "DAC-07 Baridhara", 1000)])
    rows = _rows(("Baridhara", 1, "ISSUE", "0A1111", 1000, "BDT"),
                 ("Baridhara", 31, "ISSUE", "0A2222", 1000, "BDT"),
                 ("Baridhara", 1, "ISSUE", "0A8888", 5000, "BDT"),
                 ("Baridhara", 15, "ISSUE", "0A9999", 5000, "BDT"))
    res = cr.reconcile(data, rows, MAP, filed_days={"Baridhara": {1, 15, 31}})
    notes = {f.locator: f.note for f in res.of(cr.NOT_IN_SYSTEM)}
    assert "adjacent month" in notes["0A8888"]      # day 1
    assert "adjacent month" not in notes["0A9999"]  # mid-month, a real ghost


def test_partial_coverage_only_judges_the_days_that_are_there(tmp_path):
    """Data ending mid-month must not report the rest of the month as missing."""
    data = _data(tmp_path, [
        _line(1, "Ticket payment", "0A1111", "DAC-07 Baridhara", 1000),
        _line(2, "Ticket payment", "0A2222", "DAC-07 Baridhara", 1000)])
    rows = _rows(("Baridhara", 1, "ISSUE", "0A1111", 1000, "BDT"),
                 ("Baridhara", 2, "ISSUE", "0A2222", 1000, "BDT"),
                 ("Baridhara", 20, "ISSUE", "0A3333", 9000, "BDT"))
    res = cr.reconcile(data, rows, MAP,
                       filed_days={"Baridhara": {1, 2, 20}})
    assert res.of(cr.UNREPORTED) == []
    assert res.of(cr.NOT_IN_SYSTEM) == []     # day 20 is outside the window
    assert res.first_day.day == 1 and res.last_day.day == 2


# --------------------------------------------------------------------------
# a counter that submitted nothing still belongs in the gap report
# --------------------------------------------------------------------------
def test_a_counter_that_sent_no_workbook_is_in_the_gap_table(tmp_path):
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "DAC-07 Baridhara", 10000),
        _line(16, "Ticket payment", "0A2222", "DAC-01 Airport (Dhaka)", 40000),
        _line(17, "Ticket payment", "0A3333", "DAC-01 Airport (Dhaka)", 60000)])
    rows = _rows(("Baridhara", 16, "ISSUE", "0A1111", 10000, "BDT"))
    res = cr.reconcile(data, rows, MAP, filed_days={"Baridhara": {16, 17}})

    assert "DAC-01 Airport (Dhaka)" in res.per_counter
    v = res.per_counter["DAC-01 Airport (Dhaka)"]
    assert v["sys_n"] == 2
    assert v["sys_amt"] == pytest.approx(100_000)
    assert v["not_submitted"] == 1
    assert res.not_submitted_amount == pytest.approx(100_000)
    assert {f.counter for f in res.not_submitted} == {"DAC-01 Airport (Dhaka)"}


def test_the_three_kinds_of_missing_do_not_overlap(tmp_path):
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "DAC-07 Baridhara", 10000),
        _line(17, "Ticket payment", "0A2222", "DAC-07 Baridhara", 20000),
        _line(16, "Ticket payment", "0A3333", "DAC-01 Airport (Dhaka)", 40000)])
    # Baridhara filed the 16th only, and left 0A1111 off it
    rows = _rows(("Baridhara", 16, "ISSUE", "0A9999", 500, "BDT"))
    res = cr.reconcile(data, rows, MAP, filed_days={"Baridhara": {16}})

    omitted = {f.locator for f in res.omitted}
    unfiled = {f.locator for f in res.unfiled}
    unsent = {f.locator for f in res.not_submitted}
    assert omitted == {"0A1111"}      # a sheet existed and it is not on it
    assert unfiled == {"0A2222"}      # no sheet for that day
    assert unsent == {"0A3333"}       # no workbook for that counter at all
    assert not (omitted & unfiled) and not (unfiled & unsent)
    assert len(res.of(cr.UNREPORTED)) == 3


def test_gds_and_web_channels_are_never_reported_as_missing_counters(tmp_path):
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "EXTRANET AGENC.", 90000),
        _line(16, "Ticket payment", "0A2222", "WEB", 50000),
        _line(16, "Ticket payment", "0A3333", "Galileo 1G 1G", 70000)])
    res = cr.reconcile(data, [], MAP)
    assert res.not_submitted == []
    assert res.per_counter == {}


def test_an_unsubmitted_counter_is_ranked_with_the_rest_by_what_is_missing(tmp_path):
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "DAC-01 Airport (Dhaka)", 900000),
        _line(16, "Ticket payment", "0A2222", "DAC-07 Baridhara", 1000)])
    res = cr.reconcile(data, [], MAP, filed_days={"Baridhara": {16}})
    worst = max(res.per_counter.items(),
                key=lambda kv: kv[1].get("unrep_amt", 0) + kv[1].get("unfiled_amt", 0))
    assert worst[0] == "DAC-01 Airport (Dhaka)"


def test_unsubmitted_sales_are_still_attributed_to_the_system_agent(tmp_path):
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "DAC-01 Airport (Dhaka)", 5000,
              agent="R KRISHNO")])
    res = cr.reconcile(data, [], MAP)
    assert res.per_agent["R KRISHNO"] == {"n": 1, "amt": 5000}


def test_missing_value_is_counted_even_for_a_local_currency_counter(tmp_path):
    """A MISSING sale has no counter-side figure to be incomparable with.
    Excluding those counters hid 8,992,113, most of it one that filed nothing."""
    lines, rows = [], []
    for i in range(1, 5):                      # four matched pairs at 30x
        loc = f"0A11{i:02d}"
        lines.append(_line(16, "Ticket payment", loc, "INT Doha (Qatar)", 30000))
        rows.append(("DOH", 16, "ISSUE", loc, 1000, "BDT"))
    # and one the counter never wrote down at all
    lines.append(_line(16, "Ticket payment", "0A9999", "INT Doha (Qatar)", 500000))
    data = _data(tmp_path, lines)
    res = cr.reconcile(data, _rows(*rows), {"DOH": "INT Doha (Qatar)"},
                       filed_days={"DOH": {16}})

    assert "DOH" in res.currency_suspect          # its own sheet is not comparable
    assert res.comparable("DOH") is False
    # ...but what is MISSING is the system's figure, and it counts in full
    assert res.omitted_amount == pytest.approx(500_000)
    assert res.unreported_amount == pytest.approx(500_000)


def test_a_point_of_sale_that_barely_trades_is_flagged_not_chased(tmp_path):
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "INT Riyadh City (Saudi Arabia)",
              68174)])
    res = cr.reconcile(data, [], MAP)
    v = res.per_counter["INT Riyadh City (Saudi Arabia)"]
    assert v["not_submitted"] == 1
    assert v["sys_n"] < cr.LOW_VOLUME       # the sheet notes this on the row


# --------------------------------------------------------------------------
# the exchange rate is derived, never assumed
# --------------------------------------------------------------------------
def _paired(tmp_path, pos, counter, pairs, rate, declared="LOC", extra=()):
    """`pairs` sales appearing on both sides, the counter side divided by rate."""
    lines, rows = [], []
    for i in range(pairs):
        loc = f"0A{i:04d}"
        amount = 10000 + i * 100
        lines.append(_line(16, "Ticket payment", loc, pos, amount))
        rows.append((counter, 16, "ISSUE", loc, amount / rate, declared))
    for loc, sys_amt, rep_amt, cur in extra:
        lines.append(_line(16, "Ticket payment", loc, pos, sys_amt))
        rows.append((counter, 16, "ISSUE", loc, rep_amt, cur))
    return _data(tmp_path, lines), _rows(*rows)


def test_a_rate_is_derived_from_sales_that_appear_on_both_sides(tmp_path):
    """The system holds the base-currency figure and the counter holds its own,
    so their ratio IS the rate that counter used. Nothing external is assumed."""
    data, rows = _paired(tmp_path, "INT Kuala Lumpur (Malaysia)", "KUL",
                         pairs=20, rate=30.30, declared="MYR")
    res = cr.reconcile(data, rows, {"KUL": "INT Kuala Lumpur (Malaysia)"},
                       currency_by_counter={"KUL": "MYR"},
                       filed_days={"KUL": {16}})
    info = res.rate_for("KUL", "MYR")
    assert info is not None
    assert info["rate"] == pytest.approx(30.30, rel=0.01)
    assert info["pairs"] == 20
    assert info["mixed"] is False
    # and it converts
    value, used = res.convert("KUL", "MYR", 1000)
    assert value == pytest.approx(30300, rel=0.01)
    assert used is info


def test_too_few_matched_sales_means_no_rate(tmp_path):
    """Two sales agreeing could be coincidence; there is no rate in that."""
    data, rows = _paired(tmp_path, "INT Doha (Qatar)", "DOH", pairs=2,
                         rate=34.0, declared="QAR")
    res = cr.reconcile(data, rows, {"DOH": "INT Doha (Qatar)"},
                       currency_by_counter={"DOH": "QAR"},
                       filed_days={"DOH": {16}})
    assert res.rate_for("DOH", "QAR") is None
    assert res.convert("DOH", "QAR", 1000) == (1000, None)   # left as written


def test_each_declared_currency_gets_its_own_rate(tmp_path):
    """One counter writes some sheets in SGD and some in BDT. Pooling them made
    its ratios look irreconcilable; keyed by what each ROW declares, both are
    exact."""
    already_base = [(f"0B{i:04d}", 10000, 10000, "BDT") for i in range(5)]
    data, rows = _paired(tmp_path, "INT Singapore (SIN)", "SIN", pairs=10,
                         rate=97.0, declared="SGD", extra=already_base)
    res = cr.reconcile(data, rows, {"SIN": "INT Singapore (SIN)"},
                       currency_by_counter={"SIN": "SGD"},
                       filed_days={"SIN": {16}})
    sgd = res.rate_for("SIN", "SGD")
    bdt = res.rate_for("SIN", "BDT")
    assert sgd["rate"] == pytest.approx(97.0, rel=0.01)
    assert bdt["rate"] == pytest.approx(1.0, rel=0.01)
    assert sgd["mixed"] is False and bdt["mixed"] is False


def test_rows_that_disagree_within_one_declared_currency_give_no_rate(tmp_path):
    """If rows claiming the same currency do not agree, they are not one
    currency and no single rate can stand for them."""
    noise = [(f"0C{i:04d}", 10000, 10000, "SGD") for i in range(10)]
    data, rows = _paired(tmp_path, "INT Singapore (SIN)", "SIN", pairs=10,
                         rate=97.0, declared="SGD", extra=noise)
    res = cr.reconcile(data, rows, {"SIN": "INT Singapore (SIN)"},
                       currency_by_counter={"SIN": "SGD"},
                       filed_days={"SIN": {16}})
    assert res.currency_rates[("SIN", "SGD")]["mixed"] is True
    assert res.rate_for("SIN", "SGD") is None


def test_a_base_currency_counter_needs_no_conversion(tmp_path):
    data, rows = _paired(tmp_path, "DAC-07 Baridhara", "Baridhara", pairs=20,
                         rate=1.0, declared="BDT")
    res = cr.reconcile(data, rows, MAP, currency_by_counter={"Baridhara": "BDT"},
                       filed_days={"Baridhara": {16}})
    assert res.comparable("Baridhara") is True
    assert res.currency_rates[("Baridhara", "BDT")]["rate"] == pytest.approx(1.0)
    assert "Baridhara" not in res.currency_suspect


# --------------------------------------------------------------------------
# a row written twice is not a second sale
# --------------------------------------------------------------------------
def test_several_rows_for_one_pnr_are_summed_when_that_matches(tmp_path):
    """One booking per passenger is the normal case, and summing is right."""
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "DAC-07 Baridhara", 30000)])
    rows = _rows(("Baridhara", 16, "ISSUE", "0A1111", 10000, "BDT"),
                 ("Baridhara", 16, "ISSUE", "0A1111", 10000, "BDT"),
                 ("Baridhara", 16, "ISSUE", "0A1111", 10000, "BDT"))
    res = cr.reconcile(data, rows, MAP, filed_days={"Baridhara": {16}})
    assert res.duplicate_rows == []
    assert res.per_counter["Baridhara"]["rep_amt"] == pytest.approx(30000)
    assert res.clean_matches == 1


def test_a_row_written_twice_is_detected_and_not_counted_twice(tmp_path):
    """When the SUM misses the system but a SINGLE row hits it exactly, the row
    was typed twice. 39 such rows inflated reported totals by 375,932."""
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "DAC-07 Baridhara", 31168)])
    rows = _rows(("Baridhara", 16, "ISSUE", "0A1111", 31168, "BDT"),
                 ("Baridhara", 16, "ISSUE", "0A1111", 31168, "BDT"))
    res = cr.reconcile(data, rows, MAP, filed_days={"Baridhara": {16}})
    assert len(res.duplicate_rows) == 1
    assert res.per_counter["Baridhara"]["dupe_n"] == 1
    assert res.per_counter["Baridhara"]["rep_amt"] == pytest.approx(31168)
    # and it is a clean match, not an amount mismatch
    assert res.clean_matches == 1
    assert [f for f in res.of(cr.MATCHED) if f.note] == []


def test_rows_that_neither_sum_nor_match_singly_are_left_alone(tmp_path):
    """Two rows that are simply wrong must not be silently pruned to fit."""
    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "DAC-07 Baridhara", 50000)])
    rows = _rows(("Baridhara", 16, "ISSUE", "0A1111", 10000, "BDT"),
                 ("Baridhara", 16, "ISSUE", "0A1111", 15000, "BDT"))
    res = cr.reconcile(data, rows, MAP, filed_days={"Baridhara": {16}})
    assert res.duplicate_rows == []
    assert res.per_counter["Baridhara"]["rep_amt"] == pytest.approx(25000)
    flagged = [f for f in res.of(cr.MATCHED) if f.note]
    assert len(flagged) == 1              # reported as an amount mismatch


# --------------------------------------------------------------------------
# the mapping is proved, not assumed
# --------------------------------------------------------------------------
def _sales_with(tmp_path, placements):
    """placements: (locator, point of sale) pairs the SYSTEM recorded."""
    return _data(tmp_path, [_line(16, "Ticket payment", loc, pos, 1000)
                            for loc, pos in placements])


def test_a_name_match_is_confirmed_by_the_counters_own_pnrs(tmp_path):
    data = _sales_with(tmp_path, [(f"0A{i:04d}", "DAC-07 Baridhara")
                                  for i in range(8)])
    rows = _rows(*[("Baridhara", 16, "ISSUE", f"0A{i:04d}", 1000, "BDT")
                   for i in range(8)])
    resolved, proofs = cr.verify_mapping(MAP, rows, data)
    assert resolved["Baridhara"] == "DAC-07 Baridhara"
    assert proofs["Baridhara"].verdict == cr.CONFIRMED
    assert proofs["Baridhara"].share == pytest.approx(1.0)


def test_the_evidence_outranks_the_name(tmp_path):
    """'Uttara' matches DAC-06 by name, but if its PNRs all sit under the
    USBA-Office desk then that is the desk it is."""
    data = _sales_with(tmp_path, [(f"0A{i:04d}", "DAC-17 Uttara USBA-Office")
                                  for i in range(8)])
    rows = _rows(*[("Uttara", 16, "ISSUE", f"0A{i:04d}", 1000, "BDT")
                   for i in range(8)])
    resolved, proofs = cr.verify_mapping({"Uttara": "DAC-06 Uttara"}, rows, data)
    assert resolved["Uttara"] == "DAC-17 Uttara USBA-Office"
    assert proofs["Uttara"].verdict == cr.CORRECTED
    assert proofs["Uttara"].suggested == "DAC-06 Uttara"


def test_a_counter_working_two_desks_is_referred_not_guessed(tmp_path):
    placements = [(f"0A{i:04d}", "ZYL-2 Sylhet City") for i in range(5)]
    placements += [(f"0B{i:04d}", "ZYL-1 Airport (Sylhet)") for i in range(5)]
    data = _sales_with(tmp_path, placements)
    rows = _rows(*[("ZYL", 16, "ISSUE", loc, 1000, "BDT")
                   for loc, _pos in placements])
    _resolved, proofs = cr.verify_mapping({"ZYL": "ZYL-2 Sylhet City"}, rows, data)
    assert proofs["ZYL"].verdict == cr.SPLIT
    assert proofs["ZYL"].rival


def test_too_few_pnrs_to_check_says_so_rather_than_claiming_proof(tmp_path):
    data = _sales_with(tmp_path, [("0A0001", "INT Doha (Qatar)")])
    rows = _rows(("DOH", 16, "ISSUE", "0A0001", 1000, "BDT"))
    _resolved, proofs = cr.verify_mapping({"DOH": "INT Doha (Qatar)"}, rows, data)
    assert proofs["DOH"].verdict == cr.UNPROVEN


def test_gds_placements_never_prove_a_counter(tmp_path):
    data = _sales_with(tmp_path, [(f"0A{i:04d}", "EXTRANET AGENC.")
                                  for i in range(8)])
    rows = _rows(*[("Baridhara", 16, "ISSUE", f"0A{i:04d}", 1000, "BDT")
                   for i in range(8)])
    _resolved, proofs = cr.verify_mapping(MAP, rows, data)
    assert proofs["Baridhara"].verdict == cr.UNPROVEN


def test_a_human_decision_is_kept_and_reloaded(tmp_path):
    path = tmp_path / "counter_points_of_sale.json"
    proofs = {"DOH": cr.MappingProof("DOH", "INT Doha (Qatar)",
                                     "INT Doha (Qatar)", cr.UNPROVEN, 0, 0)}
    cr.save_mapping(path, {"DOH": "INT Doha (Qatar)"}, proofs)
    assert path.is_file()
    assert cr.load_mapping_overrides(path) == {"DOH": "INT Doha (Qatar)"}

    # a human edits it; that decision must survive
    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    data["counters"]["DOH"]["point_of_sale"] = "INT Muscat (Oman)"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert cr.load_mapping_overrides(path) == {"DOH": "INT Muscat (Oman)"}


def test_a_missing_or_broken_mapping_file_is_not_an_error(tmp_path):
    assert cr.load_mapping_overrides(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert cr.load_mapping_overrides(bad) == {}


# --------------------------------------------------------------------------
# a human can confirm a point of sale IS a counter
# --------------------------------------------------------------------------
def test_a_confirmed_counter_is_not_excused_for_being_small(tmp_path):
    """INT Riyadh City sold once all month because it is short staffed, not
    because it is a kiosk. Volume cannot tell the two apart."""
    path = tmp_path / "map.json"
    cr.save_mapping(path, {}, {}, known=["INT Riyadh City (Saudi Arabia)"])
    assert cr.load_known_counters(path) == {"INT Riyadh City (Saudi Arabia)"}

    data = _data(tmp_path, [
        _line(16, "Ticket payment", "0A1111", "INT Riyadh City (Saudi Arabia)",
              68174)])
    res = cr.reconcile(data, [], MAP,
                       known_counters=cr.load_known_counters(path))
    assert res.known_counters == {"INT Riyadh City (Saudi Arabia)"}
    assert res.per_counter["INT Riyadh City (Saudi Arabia)"]["not_submitted"]


def test_known_counters_survive_a_rewrite(tmp_path):
    """The file is rewritten every run; a human's list must not be lost."""
    path = tmp_path / "map.json"
    cr.save_mapping(path, {"A": "DAC-01 Airport (Dhaka)"}, {},
                    known=["DAC-01 Airport (Dhaka)"])
    cr.save_mapping(path, {"A": "DAC-01 Airport (Dhaka)"}, {},
                    known=cr.load_known_counters(path))
    assert cr.load_known_counters(path) == {"DAC-01 Airport (Dhaka)"}


def test_a_missing_known_counters_list_is_not_an_error(tmp_path):
    assert cr.load_known_counters(tmp_path / "nope.json") == set()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert cr.load_known_counters(bad) == set()


# --- the hand-confirmed counter list ----------------------------------------

def test_a_damaged_confirmation_file_is_ignored_not_fatal(tmp_path):
    """The list is typed by a person into a JSON file, so it will be damaged
    eventually. A report must not fail because of it."""
    for body in ("{{{ not json", '["DAC-01"]', '{"known_counters": null}',
                 '{"known_counters": [1, 2]}', "", "null"):
        p = tmp_path / "m.json"
        p.write_text(body, encoding="utf-8")
        assert isinstance(cr.load_known_counters(p), set)
    assert cr.load_known_counters(tmp_path / "absent" / "m.json") == set()


def test_a_confirmation_typed_with_stray_spacing_still_counts(tmp_path):
    """A trailing space is the difference between confirming a counter and
    silently confirming nothing."""
    p = tmp_path / "m.json"
    p.write_text('{"known_counters": ["  DAC-01 Airport (Dhaka)  ", "", " "]}',
                 encoding="utf-8")
    assert cr.load_known_counters(p) == {"DAC-01 Airport (Dhaka)"}


def test_a_confirmation_survives_a_later_save_that_does_not_mention_it(tmp_path):
    p = tmp_path / "m.json"
    cr.save_mapping(p, {"Uttara": "DAC-09 Uttara"}, {}, known=["DAC-01 Airport"])
    cr.save_mapping(p, {"Uttara": "DAC-09 Uttara"}, {})
    assert cr.load_known_counters(p) == {"DAC-01 Airport"}
    cr.save_mapping(p, {}, {}, known=["INT Riyadh City"])
    assert cr.load_known_counters(p) == {"DAC-01 Airport", "INT Riyadh City"}


def test_confirming_a_counter_changes_the_note_and_not_a_number(tmp_path):
    """INT Riyadh City sold once all month because it is short staffed, not
    because it is a kiosk. Saying so must not move any figure."""
    p = tmp_path / "sales.xlsx"
    _sales_report(p, [_line(16, "Ticket payment", "0A1111",
                            "INT Riyadh City (Saudi Arabia)", 10000)])
    sales = cr.read_sales_report(p)

    plain = cr.reconcile(sales, [], {})
    confirmed = cr.reconcile(sales, [], {},
                             known_counters=["INT Riyadh City (Saudi Arabia)"])
    assert ([(f.kind, f.system_amount, f.reported_amount)
             for f in plain.findings]
            == [(f.kind, f.system_amount, f.reported_amount)
                for f in confirmed.findings])
    assert plain.unreported_amount == confirmed.unreported_amount
    assert confirmed.is_known("INT Riyadh City (Saudi Arabia)")
    assert not plain.is_known("INT Riyadh City (Saudi Arabia)")


def test_a_confirmation_matches_however_the_person_typed_it(tmp_path):
    p = tmp_path / "sales.xlsx"
    _sales_report(p, [_line(16, "Ticket payment", "0A1111",
                            "INT Riyadh City (Saudi Arabia)", 10000)])
    sales = cr.read_sales_report(p)
    res = cr.reconcile(sales, [], {},
                       known_counters=["  int riyadh CITY (saudi arabia) "])
    assert res.is_known("INT Riyadh City (Saudi Arabia)")
    assert res.known_unmatched == []


def test_a_confirmation_that_matches_nothing_is_named(tmp_path):
    """Doing nothing quietly is how a typo in a hand-edited file survives for
    months. The report says the confirmation did not land."""
    p = tmp_path / "sales.xlsx"
    _sales_report(p, [_line(16, "Ticket payment", "0A1111",
                            "INT Riyadh City (Saudi Arabia)", 10000)])
    sales = cr.read_sales_report(p)
    res = cr.reconcile(sales, [], {}, known_counters=["INT Riyad City"])
    assert res.known_unmatched == ["INT Riyad City"]
    assert not res.is_known("INT Riyadh City (Saudi Arabia)")
