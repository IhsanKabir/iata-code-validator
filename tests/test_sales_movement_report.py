"""The Sales Movement workbook.

Fixtures are synthetic: the warehouse carries real agency names and revenue.
"""
from datetime import date

from openpyxl import load_workbook

from src import sales_movement as sm
from src import sales_movement_report as smr

AUG = sm.Settings(period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
                  trailing=3, threshold=0.20, floor=500_000)


def _row(customer, window, amount, **meta):
    base = {"customer": customer, "window": window, "amount": amount,
            "tickets": 10, "customer_id": "", "iata": "1234567",
            "zone": "Zone-4", "station": "DAC", "sales_person": "Rep A",
            "agent_type": "BSP", "channel": "AGENCY"}
    base.update(meta)
    return base


def _result(**kw):
    rows = []
    rows += [_row("Falling", 0, 3_000_000)] + \
        [_row("Falling", i, 10_000_000) for i in (1, 2, 3)]
    rows += [_row("SmallFall", 0, 100_000)] + \
        [_row("SmallFall", i, 900_000) for i in (1, 2, 3)]
    rows += [_row("Rising", 0, 9_000_000)] + \
        [_row("Rising", i, 3_000_000, zone="Zone-9") for i in (1, 2, 3)]
    rows += [_row("Gone", i, 4_000_000, zone="Zone-9") for i in (1, 2, 3)]
    rows += [_row("Fresh", 0, 2_000_000, sales_person="Rep B")]
    return sm.build(rows, AUG, **kw)


def _book(tmp_path, res=None):
    out = tmp_path / "movement.xlsx"
    smr.build_workbook(res or _result(), out)
    return load_workbook(out)


def _column(ws, header, limit=60):
    """Values under a header, so a test never hard-codes a column letter.

    The Summary's tables sit below a KPI strip and any warnings, so their
    row is not fixed -- the header is searched for across the whole sheet.
    """
    for r in range(1, ws.max_row + 1):
        for c in range(1, 22):
            if ws.cell(row=r, column=c).value == header:
                return [ws.cell(row=i, column=c).value
                        for i in range(r + 1, min(r + 1 + limit,
                                                  ws.max_row + 1))]
    raise AssertionError(f"no column headed {header!r}")


def _text(ws) -> str:
    return " ".join(str(c.value) for row in ws.iter_rows() for c in row
                    if c.value is not None)


# --------------------------------------------------------------------------
def test_every_group_gets_its_own_sheet(tmp_path):
    wb = _book(tmp_path)
    assert wb.sheetnames == ["Summary", "Declined", "Stopped buying",
                             "Refunded more than sold", "Grew", "New"]


def test_the_summary_states_the_settings_in_words(tmp_path):
    said = _text(_book(tmp_path)["Summary"])
    assert "20%" in said and "500,000" in said
    assert "Aug 2026" in said and "May 2026" in said


def test_the_summary_lists_the_windows_actually_compared(tmp_path):
    ws = _book(tmp_path)["Summary"]
    roles = [v for v in _column(ws, "Window") if v]
    assert roles[:2] == ["This period", "Baseline 1"]


def test_decliners_are_ranked_by_money_not_by_percentage(tmp_path):
    """SmallFall is down 89%, Falling only 70% -- but Falling lost 7,000,000
    and SmallFall lost 800,000, so Falling belongs on top."""
    names = [v for v in _column(_book(tmp_path)["Declined"],
                                "Agency / customer") if v]
    assert names == ["Falling", "SmallFall"]


def test_a_new_customer_gets_no_percentage_cell(tmp_path):
    ws = _book(tmp_path)["New"]
    assert [v for v in _column(ws, "Agency / customer") if v] == ["Fresh"]
    assert _column(ws, "Change %")[0] is None


def test_a_lapsed_customer_is_on_its_own_sheet_with_the_baseline_as_the_loss(
        tmp_path):
    ws = _book(tmp_path)["Stopped buying"]
    assert [v for v in _column(ws, "Agency / customer") if v] == ["Gone"]
    assert _column(ws, "Change (BDT)")[0] == -4_000_000


def test_an_empty_group_still_gets_a_sheet_that_says_so(tmp_path):
    res = sm.build([_row("Only", 0, 900_000)], AUG)
    ws = _book(tmp_path, res)["Declined"]
    assert "Nobody fell into this group." in _text(ws)


def test_a_thin_baseline_is_marked_rather_than_smoothed_away(tmp_path):
    rows = [_row("Patchy", 0, 200_000), _row("Patchy", 2, 3_000_000)]
    ws = _book(tmp_path, sm.build(rows, AUG))["Declined"]
    assert _column(ws, "Months traded")[0] == "1 of 3"


def test_a_warning_about_the_data_lands_at_the_top_of_the_summary(tmp_path):
    res = _result(data_last_day=date(2026, 8, 20))
    said = _text(_book(tmp_path, res)["Summary"])
    assert "READ THIS FIRST" in said
    assert "20 Aug 2026" in said


def test_the_summary_rolls_the_money_up_by_zone_and_by_sales_person(tmp_path):
    ws = _book(tmp_path)["Summary"]
    said = _text(ws)
    assert "BY ZONE" in said and "BY SALES PERSON" in said
    zones = [v for v in _column(ws, "By Zone", limit=8) if v]
    assert "Zone-9" in zones and "Zone-4" in zones
    reps = [v for v in _column(ws, "By Sales Person", limit=8) if v]
    assert "Rep A" in reps and "Rep B" in reps


def test_the_floor_is_reported_so_nobody_wonders_where_a_customer_went(
        tmp_path):
    rows = [_row("Tiny", 0, 100)] + [_row("Tiny", i, 900) for i in (1, 2, 3)]
    res = sm.build(rows, AUG)
    assert res.below_floor == 1
    assert "1 customer(s) fell below" in _text(_book(tmp_path, res)["Summary"])


def test_the_filename_names_the_period(tmp_path):
    assert smr.default_filename(AUG) == "Sales_Movement_Aug2026.xlsx"


# --------------------------------------------------------------------------
# the cells that were printing a number the caption says is never printed
# --------------------------------------------------------------------------
def test_the_stopped_buying_sheet_prints_no_percentage_at_all(tmp_path):
    """The caption on this sheet reads 'No percentage is printed'. It was
    writing -1 into every Change % cell, which Excel renders as -100%."""
    ws = _book(tmp_path)["Stopped buying"]
    assert [v for v in _column(ws, "Agency / customer") if v] == ["Gone"]
    assert _column(ws, "Change %")[0] is None


def test_a_refunder_gets_its_own_sheet_with_the_real_negative_figure(tmp_path):
    rows = [_row("Refunder", 0, -250_000)] + \
        [_row("Refunder", i, 4_000_000) for i in (1, 2, 3)]
    ws = _book(tmp_path, sm.build(rows, AUG))["Refunded more than sold"]
    assert [v for v in _column(ws, "Agency / customer") if v] == ["Refunder"]
    assert _column(ws, "This period (BDT)")[0] == -250_000
    assert _column(ws, "Change (BDT)")[0] == -4_250_000   # exceeds the baseline
    assert _column(ws, "Change %")[0] is None


def test_a_refunder_is_never_filed_under_stopped_buying(tmp_path):
    rows = [_row("Refunder", 0, -250_000)] + \
        [_row("Refunder", i, 4_000_000) for i in (1, 2, 3)]
    wb = _book(tmp_path, sm.build(rows, AUG))
    assert "Nobody fell into this group." in _text(wb["Stopped buying"])


# --------------------------------------------------------------------------
# a ticket count is not an amount of money
# --------------------------------------------------------------------------
def _tickets_settings():
    return sm.Settings(period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
                       trailing=3, threshold=0.20, floor=50,
                       measure=sm.MEASURE_TICKETS)


def test_the_tickets_measure_does_not_label_its_columns_bdt(tmp_path):
    """Choosing 'Tickets issued' used to present a count of tickets under a
    heading that said BDT, on every sheet in the workbook."""
    s = _tickets_settings()
    rows = [_row("Falling", 0, 300)] + \
        [_row("Falling", i, 1_000) for i in (1, 2, 3)]
    wb = _book(tmp_path, sm.build(rows, s))
    ws = wb["Declined"]
    assert _column(ws, "Baseline average (tickets)")[0] == 1_000
    assert _column(ws, "This period (tickets)")[0] == 300
    assert "(BDT)" not in _text(ws)


def test_the_tickets_summary_does_not_say_money(tmp_path):
    s = _tickets_settings()
    rows = [_row("Falling", 0, 300)] + \
        [_row("Falling", i, 1_000) for i in (1, 2, 3)]
    said = _text(_book(tmp_path, sm.build(rows, s))["Summary"])
    assert "LOST (TICKETS)" in said.upper()
    assert "MONEY LOST" not in said.upper()
    assert "50 tickets" in said            # the floor is stated in its unit


# --------------------------------------------------------------------------
# these sheets are built to be forwarded, and the names on them are typed by
# people -- the warehouse already holds one starting with a minus sign
# --------------------------------------------------------------------------
def test_a_name_that_starts_like_a_formula_is_not_evaluated_by_excel(tmp_path):
    rows = [_row("-Ul-Islam Md Armaan", 0, 100_000)] + \
        [_row("-Ul-Islam Md Armaan", i, 4_000_000) for i in (1, 2, 3)]
    ws = _book(tmp_path, sm.build(rows, AUG))["Declined"]
    got = _column(ws, "Agency / customer")[0]
    assert got.startswith("'"), "Excel would evaluate this as a formula"
    assert "Ul-Islam Md Armaan" in got


def test_an_ordinary_name_is_left_exactly_as_it_is(tmp_path):
    ws = _book(tmp_path)["Declined"]
    assert _column(ws, "Agency / customer")[0] == "Falling"


def test_numbers_and_dates_are_not_touched_by_the_guard(tmp_path):
    from src.counter_master import _safe_text
    assert _safe_text(-4_000_000) == -4_000_000
    assert _safe_text(None) is None
    assert _safe_text(date(2026, 8, 31)) == date(2026, 8, 31)
