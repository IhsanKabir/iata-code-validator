"""Counter Activity master sheet.

The fixtures are synthetic: real counter workbooks carry customer mobile numbers
and staff names, which must not land in the repo. Each case below mirrors a
defect actually found in the August 2026 files.
"""
from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from src import counter_master as cm


# --------------------------------------------------------------------------
# value typing
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [
    (12345, 12345.0),
    ("12,345", 12345.0),
    ("3847", 3847.0),
    (0, None),
    (None, None),
    ("", None),
    ("01712414786", None),       # a customer mobile, not money
    ("+8801712414786", None),
    ("4748****6144", None),      # a masked card reference
    ("0172***499", None),
    ("APPR CODE:370063", None),
    (True, None),               # a bool is not an amount
])
def test_money_accepts_only_real_amounts(raw, expected):
    assert cm._money(raw) == expected


@pytest.mark.parametrize("header, channel", [
    ("Cash", "Cash"),
    ("CASH", "Cash"),
    ("Cash/CHEQUE", "Cash"),
    ("Bkash", "bKash"),
    ("Bkash/Nagad", "bKash"),
    ("Card", "Card"),
    ("Cheque", "Cheque"),
    ("Invoice", "Invoice"),
    ("Alipay", "Alipay"),
    ("Wechat", "WeChat"),
    ("Bank", "Bank"),
    # one counter spells a single channel seven different ways
    ("Online Transaction to scb", "Bank"),
    ("Online Transacttion to Scb", "Bank"),
    ("Online Transaction deposited to scb", "Bank"),
    ("Online Transaction  on SCB", "Bank"),
    ("online transaction on scb", "Bank"),
])
def test_channel_of_tolerates_how_it_was_typed(header, channel):
    assert cm._channel_of(header) == channel


@pytest.mark.parametrize("header", [
    "Bkash/ Card No",           # holds a reference, not an amount
    "Transaction no (if Applicable)",
    "PNR",
    "Employee Name",
    "",
])
def test_channel_of_rejects_reference_columns(header):
    assert cm._channel_of(header) is None


# --------------------------------------------------------------------------
# counter naming
# --------------------------------------------------------------------------
@pytest.mark.parametrize("stem, expected", [
    ("baridhara counter aug 26", "Baridhara"),
    ("DXB Counterr aug 26", "DXB"),
    ("RJH Counte auh 26", "RJH"),          # 'auh' is a typo for 'aug' HERE
    ("AUH Counter aug 26", "AUH"),         # ...but AUH is also a real station
    ("Mirpur Counter Aug 26", "Mirpur"),
    ("cox bazar counter aug 26", "Cox Bazar"),
])
def test_counter_name_survives_hand_typed_filenames(stem, expected):
    assert cm._clean_counter_name(stem) == expected


# --------------------------------------------------------------------------
# dating
# --------------------------------------------------------------------------
def test_day_comes_from_the_sheet_name():
    grid = [["Counter Daily Activities"], []]
    assert cm._resolve_day("06 AUG", grid, 5, 8) == (6, "sheetname")


def test_day_comes_from_the_title_when_the_name_has_none():
    grid = [["Counter Daily Activities of  01 Aug 2026"], []]
    assert cm._resolve_day("Sheet2", grid, 1, 8) == (1, "title")


def test_a_stale_month_in_the_title_is_flagged_but_the_day_is_kept():
    """Sheet3 of an AUGUST workbook says '2 Jul 2026' -- the template was copied
    from last month. The day is still right; the month must not be believed."""
    grid = [["Counter Daily Activities of  2 Jul 2026"], []]
    day, source = cm._resolve_day("Sheet3", grid, 2, 8)
    assert day == 2
    assert source == "title(stale-month)"


def test_ordinal_is_the_last_resort():
    grid = [["Counter Daily Activities"], []]
    assert cm._resolve_day("Sheet8", grid, 7, 8) == (7, "ordinal")


# --------------------------------------------------------------------------
# payment reconciliation
# --------------------------------------------------------------------------
def _cmap(headers):
    return {cm._hkey(h): j for j, h in enumerate(headers) if h}


def test_payments_are_credited_when_they_add_up():
    headers = ["Sales Amount (BDT)", "Cash", "Bkash", "Card"]
    row = [10000, 6000, 4000, None]
    pays, ok, _why = cm._split_payments(row, _cmap(headers), 10000, [], "X",
                                  amount_col=0)
    assert ok is True
    assert pays == {"Cash": 6000.0, "bKash": 4000.0}


def test_a_row_shifted_one_column_off_its_header_still_reconciles():
    """A merged cell above pushes the data one column right of its own header."""
    headers = ["Sales Amount (BDT)", "Cash", "Bkash", "Card"]
    row = [None, 10000, 10000, None]     # amount and cash both sit one to the right
    pays, ok, _why = cm._split_payments(row, _cmap(headers), 10000, [], "X",
                                  amount_col=1, header_col=0)
    assert ok is True
    assert pays == {"Cash": 10000.0}   # cash, NOT the bKash column it sits under


def test_payments_that_do_not_reconcile_are_left_unallocated():
    headers = ["Sales Amount (BDT)", "Cash", "Bkash"]
    row = [10000, 6000, 1000]            # 7,000 collected against a 10,000 sale
    issues = []
    _pays, ok, _why = cm._split_payments(row, _cmap(headers), 10000, issues, "X",
                                   amount_col=0, header_col=0)
    assert ok is False
    assert [i.kind for i in issues] == ["payment_mismatch"]


def test_a_card_number_is_never_read_as_a_payment():
    headers = ["Sales Amount (BDT)", "Cash", "Bkash/ Card No"]
    row = [8098, 8098, "4748****6144"]
    pays, ok, _why = cm._split_payments(row, _cmap(headers), 8098, [], "X",
                                  amount_col=0, header_col=0)
    assert ok is True
    assert pays == {"Cash": 8098.0}


# --------------------------------------------------------------------------
# end to end
# --------------------------------------------------------------------------
HEADERS = ["Counter Name", "Action", "Count PNR", "Employee Name ", " Employee ID",
           "PNR", "", "Customer Mobile No", "", "Sales Amount (BDT)", "Cash",
           "Bkash", "Card", "Bkash/ Card No"]


def _day_sheet(wb, title, rows, day_label):
    ws = wb.create_sheet(title)
    ws.append([f"Counter Daily Activities of {day_label}"])
    ws.append(HEADERS)
    for r in rows:
        ws.append(r)
    return ws


def _make_counter_workbook(path, *, drift=False, extra_days=0):
    wb = Workbook()
    wb.remove(wb.active)
    rows = [
        ["TEST SALES", "Ticket Issue", 2, "ALEX ROY", "USBA-90001", "0A1111",
         None, "01700000001", None, 10000, 10000, None, None, None],
        [None, None, None, "ALEX ROY", "USBA-90001", "0A2222",
         None, "01700000002", None, 5000, None, 5000, None, None],
        [None, None, None, "SAM LEE", "USBA-90002", "0A3333",
         None, "01700000003", None, 4000, 4000, None, None, None],
    ]
    if drift:
        # every data row sits one column right of its header, as a merged cell
        # above the block causes in the real files
        rows = [[None] + r[:-1] for r in rows]
    _day_sheet(wb, "01 AUG", rows, "01 Aug 2026")
    _day_sheet(wb, "02 AUG", [
        [None, "Ticket Refund", 1, "SAM LEE", "USBA-90002", "0A4444",
         None, "01700000004", None, 1500, 1500, None, None, None],
    ], "02 Aug 2026")
    for d in range(3, 3 + extra_days):        # pad the month to lift coverage
        _day_sheet(wb, f"{d:02d} AUG", [
            [None, "Ticket Issue", 1, "ALEX ROY", "USBA-90001", f"0A5{d:03d}",
             None, "01700000009", None, 1000, 1000, None, None, None],
        ], f"{d:02d} Aug 2026")
    wb.save(path)


def test_master_sheet_totals_and_layout(tmp_path):
    folder = tmp_path / "counters"
    folder.mkdir()
    _make_counter_workbook(folder / "alpha counter aug 26.xlsx")
    out = tmp_path / "master.xlsx"

    seen = []
    result = cm.build_from_folder(folder, out, month=8, year=2026,
                                  progress_cb=lambda d, t, n: seen.append(n))

    assert result.path == out
    assert result.counters == 1
    assert result.employees == 2
    assert result.rows == 4                     # 3 issues + 1 refund
    assert seen == ["alpha counter aug 26"]
    # 19,000 issued less a 1,500 refund
    assert result.net == pytest.approx(17_500)
    assert result.unallocated_pct == pytest.approx(0.0)

    wb = load_workbook(out)
    assert wb.sheetnames == ["Master"]          # ONE sheet, by design
    text = "\n".join(
        str(c.value) for row in wb["Master"].iter_rows() for c in row
        if c.value is not None)
    for section in ("AT A GLANCE", "PAYMENT TYPES", "COUNTER LEAGUE TABLE",
                    "EMPLOYEE SCORECARD", "DATA QUALITY"):
        assert section in text
    assert "Alpha" in text                      # counter name cleaned
    assert "USBA-90001" in text
    wb.close()


def test_column_drift_does_not_change_the_totals(tmp_path):
    """The same figures, with every row shifted one column off its header."""
    straight, shifted = tmp_path / "a", tmp_path / "b"
    straight.mkdir()
    shifted.mkdir()
    _make_counter_workbook(straight / "alpha counter aug 26.xlsx")
    _make_counter_workbook(shifted / "alpha counter aug 26.xlsx", drift=True)

    a = cm.build_from_folder(straight, tmp_path / "a.xlsx", month=8, year=2026)
    b = cm.build_from_folder(shifted, tmp_path / "b.xlsx", month=8, year=2026)

    assert b.rows == a.rows
    assert b.net == a.net
    assert b.employees == a.employees


def test_a_counter_that_filed_nothing_is_reported_not_guessed(tmp_path):
    """A counter that files nothing must appear as a finding, not vanish -- and it
    must not drag a counter that DID file into the same bucket."""
    folder = tmp_path / "counters"
    folder.mkdir()
    _make_counter_workbook(folder / "alpha counter aug 26.xlsx", extra_days=24)
    quiet = Workbook()
    quiet.active.title = "01 AUG"
    quiet.save(folder / "quiet counter aug 26.xlsx")

    result = cm.build_from_folder(folder, tmp_path / "m.xlsx", month=8, year=2026)

    assert result.counters == 2
    assert result.not_reporting == ("Quiet",)


def test_stop_flag_abandons_the_run_but_still_writes(tmp_path):
    folder = tmp_path / "counters"
    folder.mkdir()
    for name in ("alpha", "beta", "gamma"):
        _make_counter_workbook(folder / f"{name} counter aug 26.xlsx")

    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 2          # let one workbook through, then stop

    result = cm.build_from_folder(folder, tmp_path / "m.xlsx", month=8, year=2026,
                                  stop_flag=stop)
    assert result.stopped is True
    assert result.counters < 3
    assert result.path.exists()


def test_a_single_file_can_be_the_whole_input(tmp_path):
    """A counter that re-sends its workbook alone must be usable on its own."""
    one = tmp_path / "alpha counter aug 26.xlsx"
    _make_counter_workbook(one)
    assert cm.resolve_counter_inputs(one) == [one]
    assert cm.resolve_counter_inputs(str(one)) == [one]

    result = cm.build_from_inputs(one, tmp_path / "m.xlsx", month=8, year=2026)
    assert result.counters == 1
    assert result.rows == 4


def test_files_and_folders_can_be_mixed_and_are_de_duplicated(tmp_path):
    folder = tmp_path / "counters"
    folder.mkdir()
    a = folder / "alpha counter aug 26.xlsx"
    b = folder / "beta counter aug 26.xlsx"
    loose = tmp_path / "gamma counter aug 26.xlsx"
    for p in (a, b, loose):
        _make_counter_workbook(p)

    # the folder ALREADY contains a -- naming it again must not read it twice
    found = cm.resolve_counter_inputs([folder, a, loose])
    assert [p.name for p in found] == [a.name, b.name, loose.name]

    result = cm.build_from_inputs([folder, a, loose], tmp_path / "m.xlsx",
                                  month=8, year=2026)
    assert result.counters == 3
    assert result.rows == 12          # 4 rows each, alpha counted once


def test_resolver_ignores_junk_selections(tmp_path):
    _make_counter_workbook(tmp_path / "alpha counter aug 26.xlsx")
    (tmp_path / "notes.txt").write_text("not a workbook", encoding="utf-8")
    (tmp_path / "~$alpha counter aug 26.xlsx").write_bytes(b"")

    assert cm.resolve_counter_inputs(tmp_path / "notes.txt") == []
    assert cm.resolve_counter_inputs(tmp_path / "nope.xlsx") == []
    assert cm.resolve_counter_inputs("") == []
    assert cm.resolve_counter_inputs(None) == []
    # a folder selection still filters to real workbooks
    assert [p.name for p in cm.resolve_counter_inputs(tmp_path)] == [
        "alpha counter aug 26.xlsx"]


def test_quoted_paths_from_copy_paste_are_accepted(tmp_path):
    one = tmp_path / "alpha counter aug 26.xlsx"
    _make_counter_workbook(one)
    assert cm.resolve_counter_inputs(f'"{one}"') == [one]


def test_lock_files_are_ignored(tmp_path):
    (tmp_path / "~$alpha counter aug 26.xlsx").write_bytes(b"")
    _make_counter_workbook(tmp_path / "alpha counter aug 26.xlsx")
    found = cm.find_counter_workbooks(tmp_path)
    assert [p.name for p in found] == ["alpha counter aug 26.xlsx"]


def test_master_path_names_the_month(tmp_path):
    p = cm.build_master_path(tmp_path, 8, 2026)
    assert p.name == "Counter_Master_Aug2026.xlsx"
    assert p.parent == tmp_path


def test_an_empty_folder_is_an_error_not_an_empty_report(tmp_path):
    with pytest.raises(ValueError, match="No .xlsx"):
        cm.build_from_folder(tmp_path, tmp_path / "m.xlsx", month=8, year=2026)


def test_days_in_month_is_respected_for_coverage(tmp_path):
    """February has 28 days, so one filed day is worth more coverage than in August."""
    folder = tmp_path / "c"
    folder.mkdir()
    _make_counter_workbook(folder / "alpha counter feb 26.xlsx")
    feb = cm.build_from_folder(folder, tmp_path / "f.xlsx", month=2, year=2026)
    aug = cm.build_from_folder(folder, tmp_path / "g.xlsx", month=8, year=2026)
    assert feb.coverage > aug.coverage
    assert date(2026, 2, 1)      # sanity: the month is real


# --------------------------------------------------------------------------
# accuracy: a sale must never be dropped because a field was left blank
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [
    ("35,548 INR", 35548.0),      # an overseas counter labels its own currency
    ("BDT 1234", 1234.0),
    ("1234 TK", 1234.0),
    ("RM 560", 560.0),
    ("560 MYR", 560.0),
    ("1,234.50", 1234.5),
    ("DH696NM88H", None),         # a ticket reference is not 696
    ("DHM9OJZXHL", None),
    ("APPR CODE:370063", None),
    ("B- 01789864543", None),
])
def test_amounts_wearing_a_currency_label(raw, expected):
    assert cm._money(raw) == expected


def test_a_sale_row_with_no_employee_id_is_kept_and_named(tmp_path):
    """Staff often write a colleague's name and leave the ID blank. Anchoring on
    the ID dropped 115,314 from one day, against that sheet's own printed total.
    """
    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("01 AUG")
    ws.append(["Counter Daily Activities of 01 Aug 2026"])
    ws.append(HEADERS)
    ws.append(["TEST SALES", "Ticket Issue", 2, "ALEX ROY", "USBA-90001",
               "0A1111", None, "01700000001", None, 10000, 10000, None, None,
               None])
    # same person, ID left blank
    ws.append([None, None, None, "ALEX ROY", None, "0A2222",
               None, "01700000002", None, 9404, 9404, None, None, None])
    # someone whose ID never appears anywhere
    ws.append([None, None, None, "PAT SINGH", None, "0A3333",
               None, "01700000003", None, 105910, 105910, None, None, None])
    wb.save(tmp_path / "alpha counter aug 26.xlsx")

    rows, issues, _meta = cm.parse_workbook(
        tmp_path / "alpha counter aug 26.xlsx", 8)
    assert len(rows) == 3
    assert sum(r.amount for r in rows) == pytest.approx(125_314)
    by_pnr = {r.pnr: r for r in rows}
    # the blank ID is recovered from the row that does state it
    assert by_pnr["0A2222"].emp_id == "USBA-90001"
    # the unknown one keeps its money and is reported, not dropped
    assert by_pnr["0A3333"].emp_id == ""
    assert by_pnr["0A3333"].amount == 105910
    assert any(i.kind == "no_employee_id" for i in issues)


def test_an_amount_further_from_its_header_than_the_span_is_still_found(tmp_path):
    """One refund row sat three columns right of its own header."""
    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("01 AUG")
    ws.append(["Counter Daily Activities of 01 Aug 2026"])
    ws.append(HEADERS)
    row = [None] * 14
    row[1] = "Ticket Refund"
    row[3] = "ALEX ROY"
    row[4] = "USBA-90001"
    row[5] = "0A1111"
    row[8] = "8801865431745"        # a mobile, which must not be read as money
    row[12] = 4698.0                # the only real amount, 3 columns adrift
    ws.append(row)
    wb.save(tmp_path / "alpha counter aug 26.xlsx")

    rows, _issues, _meta = cm.parse_workbook(
        tmp_path / "alpha counter aug 26.xlsx", 8)
    assert len(rows) == 1
    assert rows[0].block == "REFUND"
    assert rows[0].amount == pytest.approx(4698)


def test_an_ambiguous_row_does_not_guess_which_number_is_the_sale(tmp_path):
    """Two candidate amounts and no header match: report nothing rather than
    pick one. A wrong amount is worse than a missing one."""
    headers = ["Sales Amount (BDT)", "Cash", "Bkash"]
    cmap = {cm._hkey(h): j for j, h in enumerate(headers) if h}
    pays, ok, why = cm._split_payments([None, None, None], cmap, 0, [], "X")
    assert ok is False
    assert pays == {}
    assert why == "no amount"


# --------------------------------------------------------------------------
# the sales-person leaderboards
# --------------------------------------------------------------------------
def _leaderboard_rows(ws, title):
    """(header, data rows, the not-ranked note) for one leaderboard."""
    start = next(k for k in range(1, ws.max_row + 1)
                 if isinstance(ws.cell(k, 1).value, str)
                 and title in str(ws.cell(k, 1).value))
    header = [ws.cell(start + 1, c).value for c in range(1, 11)]
    rows, note = [], ""
    k = start + 2
    while ws.cell(k, 1).value is not None:
        v = ws.cell(k, 1).value
        if isinstance(v, str) and "not ranked" in v:
            note = v
            break
        rows.append([ws.cell(k, c).value for c in range(1, 11)])
        k += 1
    return header, rows, note


def test_the_leaderboards_rank_by_value_and_show_a_per_day_column(tmp_path):
    folder = tmp_path / "c"
    folder.mkdir()
    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("01 AUG")
    ws.append(["Counter Daily Activities of 01 Aug 2026"])
    ws.append(HEADERS)
    # one big seller on a single day, one steady seller across two
    ws.append(["TEST SALES", "Ticket Issue", 2, "BIG EARNER", "USBA-90001",
               "0A1111", None, "01700000001", None, 500000, 500000, None, None,
               None])
    ws.append([None, None, None, "STEADY", "USBA-90002", "0A2222",
               None, "01700000002", None, 100000, 100000, None, None, None])
    ws.append([None, "Ticket Reissue", 1, "STEADY", "USBA-90002", "0A3333",
               None, "01700000003", None, 70000, 70000, None, None, None])
    ws2 = wb.create_sheet("02 AUG")
    ws2.append(["Counter Daily Activities of 02 Aug 2026"])
    ws2.append(HEADERS)
    ws2.append([None, "Ticket Issue", 1, "STEADY", "USBA-90002", "0A4444",
                None, "01700000004", None, 100000, 100000, None, None, None])
    wb.save(folder / "alpha counter aug 26.xlsx")

    out = tmp_path / "m.xlsx"
    cm.build_from_inputs(folder, out, month=8, year=2026)
    book = load_workbook(out)
    ws = book["Master"]

    header, rows, _note = _leaderboard_rows(ws, "BY VALUE ISSUED")
    assert header[4] == "Tickets issued"
    assert header[8] == "Value per active day"
    assert [r[1] for r in rows] == ["Big Earner", "Steady"]     # by value
    assert rows[0][5] == pytest.approx(500_000)
    assert rows[0][8] == pytest.approx(500_000)                 # one active day
    assert rows[1][8] == pytest.approx(100_000)                 # 200k over 2 days
    assert rows[1][9] == pytest.approx(0.4)                     # share of top

    header, rows, _note = _leaderboard_rows(ws, "BY VALUE REISSUED")
    assert header[4] == "Reissues"
    assert [r[1] for r in rows] == ["Steady"]                   # only reissuer
    assert rows[0][5] == pytest.approx(70_000)
    book.close()


def test_a_local_currency_counter_is_named_when_no_rate_can_be_derived(tmp_path):
    """With no sales data there are no matched pairs, so there is no rate to
    convert with. Ranking anyway would place it arbitrarily."""
    folder = tmp_path / "c"
    folder.mkdir()
    for name, cur, amount in (("alpha", "BDT", 100000), ("can", "CNY", 5000)):
        wb = Workbook()
        wb.remove(wb.active)
        ws = wb.create_sheet("01 AUG")
        ws.append(["Counter Daily Activities of 01 Aug 2026"])
        ws.append([h.replace("(BDT)", f"({cur})") for h in HEADERS])
        ws.append(["X", "Ticket Issue", 1, "SELLER " + name.upper(),
                   "USBA-9000" + ("1" if cur == "BDT" else "2"), "0A111" + name[0],
                   None, "01700000001", None, amount, amount, None, None, None])
        wb.save(folder / f"{name} counter aug 26.xlsx")

    out = tmp_path / "m.xlsx"
    cm.build_from_inputs(folder, out, month=8, year=2026)
    book = load_workbook(out)
    _h, rows, note = _leaderboard_rows(book["Master"], "BY VALUE ISSUED")
    assert [r[3] for r in rows] == ["Alpha"]        # only the BDT counter ranks
    assert "no rate could be derived" in note
    assert "CAN" in note          # a three-letter station keeps its capitals
    book.close()


def test_the_nameless_bucket_never_appears_in_a_leaderboard(tmp_path):
    folder = tmp_path / "c"
    folder.mkdir()
    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("01 AUG")
    ws.append(["Counter Daily Activities of 01 Aug 2026"])
    ws.append(HEADERS)
    ws.append(["X", "Ticket Issue", 1, "REAL PERSON", "USBA-90001", "0A1111",
               None, "01700000001", None, 1000, 1000, None, None, None])
    ws.append([None, None, None, None, None, "0A2222",
               None, "01700000002", None, 999000, 999000, None, None, None])
    wb.save(folder / "alpha counter aug 26.xlsx")

    out = tmp_path / "m.xlsx"
    cm.build_from_inputs(folder, out, month=8, year=2026)
    book = load_workbook(out)
    _h, rows, _n = _leaderboard_rows(book["Master"], "BY VALUE ISSUED")
    # the 999,000 belongs to nobody, so it must not top the leaderboard
    assert [r[1] for r in rows] == ["Real Person"]
    book.close()


def test_conversion_is_applied_once_and_only_once(tmp_path):
    """The rows are restated before aggregating, so the leaderboard must NOT
    multiply again. Doing so turned one agent's 1,986,546 into 60,634,814."""
    class _Recon:
        currency_suspect = {"KUL": 30.0}
        currency_rates = {("KUL", "MYR"): {"rate": 30.0, "pairs": 20,
                                           "spread": 0.0, "mixed": False,
                                           "currency": "MYR"}}

        def rate_for(self, counter, currency=None):
            return self.currency_rates.get((counter, currency))

    rows = [cm.SaleRow("KUL", 1, "ISSUE", "USBA-90001", "A", "0A1111",
                       1000.0, "MYR", {"Cash": 1000.0})]
    applied = cm._to_base(rows, _Recon(), "BDT")
    assert rows[0].amount == pytest.approx(30_000)
    assert rows[0].payments["Cash"] == pytest.approx(30_000)
    assert rows[0].currency == "BDT"
    assert rows[0].converted_rate == 30.0
    assert ("KUL", "MYR") in applied

    # running it again must be a no-op: the row is already in base currency
    cm._to_base(rows, _Recon(), "BDT")
    assert rows[0].amount == pytest.approx(30_000)


def test_a_row_with_no_amount_is_still_restated(tmp_path):
    """It has nothing to convert, but it belongs to a restated counter. Leaving
    it behind made a counter look like it was still keeping INR."""
    class _Recon:
        def rate_for(self, counter, currency=None):
            return ({"rate": 1.3, "pairs": 20, "spread": 0.0, "mixed": False,
                     "currency": "INR"} if currency == "INR" else None)

    rows = [cm.SaleRow("MAA", 1, "ISSUE", "USBA-90001", "A", "0A1111",
                       0.0, "INR"),
            cm.SaleRow("MAA", 1, "ISSUE", "USBA-90001", "A", "0A2222",
                       100.0, "INR")]
    cm._to_base(rows, _Recon(), "BDT")
    assert [r.currency for r in rows] == ["BDT", "BDT"]
    assert rows[0].amount == 0
    assert rows[1].amount == pytest.approx(130)


def test_no_rate_leaves_the_row_exactly_as_written(tmp_path):
    class _Recon:
        def rate_for(self, counter, currency=None):
            return None

    rows = [cm.SaleRow("SIN", 1, "ISSUE", "USBA-90001", "A", "0A1111",
                       500.0, "SGD")]
    applied = cm._to_base(rows, _Recon(), "BDT")
    assert rows[0].amount == 500.0
    assert rows[0].currency == "SGD"          # untouched, and so not rankable
    assert applied == {}


def test_the_zenith_lookup_survives_the_second_comparison(tmp_path, monkeypatch):
    """The comparison runs twice -- once to derive rates, once on the restated
    rows. Enriching the first pass and then replacing it made 158 live calls to
    Zenith and put none of the answers on the sheet."""
    folder = tmp_path / "c"
    folder.mkdir()
    _make_counter_workbook(folder / "alpha counter aug 26.xlsx")

    class _Sales:
        lines = []
        first_day = last_day = None
        points_of_sale = {}
        rows_read = 0
        voided = 0

    calls = []

    class _Details:
        customer_name = ""
        phone = ""
        pnr_status = "Issued"
        pax_count = 1
        booked_route = "DAC-CXB-DAC"

    def fake_lookup(_session, code):
        calls.append(code)
        return _Details()

    from src import counter_blocks, counter_reconcile

    finding = counter_reconcile.Finding(
        kind=counter_reconcile.UNREPORTED, counter="Alpha",
        pos="DAC-99 Alpha", day=date(2026, 8, 1), locator="0A1111",
        block="ISSUE", system_amount=1000.0)

    # a comparison that yields one omitted sale, however many times it runs
    def fake_reconcile(*_a, **_k):
        res = counter_reconcile.ReconResult()
        res.findings = [finding]
        res.first_day = res.last_day = date(2026, 8, 1)
        return res

    monkeypatch.setattr(counter_reconcile, "read_sales_from_warehouse",
                        lambda *a, **k: _Sales())
    monkeypatch.setattr(counter_reconcile, "find_sales_warehouse",
                        lambda *a, **k: cm_dummy_source())
    monkeypatch.setattr(counter_reconcile, "suggest_mapping",
                        lambda *a, **k: ({}, {}, [], [], {}))
    monkeypatch.setattr(counter_reconcile, "reconcile", fake_reconcile)
    monkeypatch.setattr("src.zenith_pnr_client.lookup_pnr", fake_lookup)

    cm.build_from_inputs(folder, tmp_path / "m.xlsx", month=8, year=2026,
                         use_warehouse=True, zenith_session=object())

    assert calls == ["0A1111"]
    # the enrichment must be on the finding that was actually rendered
    assert "Issued" in finding.note
    assert "DAC-CXB-DAC" in finding.note


def cm_dummy_source():
    from src.counter_reconcile import WarehouseSource
    src = WarehouseSource(path=Path("x"), kind="gold")
    src.first_day = date(2026, 8, 1)
    src.last_day = date(2026, 8, 31)
    return src


def test_the_headcount_is_the_same_everywhere_it_is_stated(tmp_path):
    """The result object drives the app's own message; the sheet drives what a
    reader sees. They said 70 and 69 for a while, because only one of them
    excluded the bucket for rows with no employee ID."""
    folder = tmp_path / "c"
    folder.mkdir()
    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("01 AUG")
    ws.append(["Counter Daily Activities of 01 Aug 2026"])
    ws.append(HEADERS)
    ws.append(["X", "Ticket Issue", 1, "REAL PERSON", "USBA-90001", "0A1111",
               None, "01700000001", None, 1000, 1000, None, None, None])
    ws.append([None, None, None, None, None, "0A2222",       # no employee id
               None, "01700000002", None, 2000, 2000, None, None, None])
    wb.save(folder / "alpha counter aug 26.xlsx")

    out = tmp_path / "m.xlsx"
    res = cm.build_from_inputs(folder, out, month=8, year=2026)
    book = load_workbook(out)
    sheet = book["Master"]
    assert res.employees == 1                      # not 2
    assert sheet.cell(6, 4).value == res.employees
    assert f"{res.employees} employees" in str(sheet.cell(2, 1).value)
    # and the nameless row's money is still counted
    assert res.net == pytest.approx(3000)
    book.close()
