"""The decline list joined to the contacts in the visit reports.

Fixtures are synthetic: the real visit reports carry agency contacts, staff
names and mobile numbers.
"""
from datetime import date

from openpyxl import load_workbook

from src import sales_call_list as scl
from src import sales_movement as sm

AUG = sm.Settings(period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
                  trailing=3, threshold=0.20)


class _Visit:
    def __init__(self, agency, *, contact="", designation="", phone="",
                 rep="", day=None):
        self.agency, self.contact = agency, contact
        self.designation, self.phone = designation, phone
        self.rep, self.day = rep, day


class _Data:
    def __init__(self, visits):
        self.visits = visits


def _row(customer, window, amount, **meta):
    base = {"customer": customer, "window": window, "amount": amount,
            "tickets": 5, "customer_id": f"C-{customer}", "iata": "1234567",
            "zone": "Zone-4", "station": "DAC", "sales_person": "Rep A",
            "agent_type": "BSP", "channel": "AGENCY"}
    base.update(meta)
    return base


def _result():
    rows = []
    rows += [_row("AB Travels", 0, 1_000_000,
                  last_bought=date(2026, 8, 28))] + \
        [_row("AB Travels", i, 9_000_000) for i in (1, 2, 3)]
    rows += [_row("Quiet Tours", i, 3_000_000,
                  last_bought=date(2026, 5, 2)) for i in (1, 2, 3)]
    rows += [_row("Small Dip", 0, 100_000,
                  last_bought=date(2026, 8, 20))] + \
        [_row("Small Dip", i, 900_000) for i in (1, 2, 3)]
    rows += [_row("Growing", 0, 9_000_000)] + \
        [_row("Growing", i, 1_000_000) for i in (1, 2, 3)]
    return sm.build(rows, AUG)


CONTACTS = _Data([
    _Visit("AB Travel", contact="Mr Rahman", designation="Manager",
           phone="01711000000", rep="Karim", day=date(2026, 7, 10)),
    _Visit("Quiet Tours Ltd.", contact="Ms Akter", phone="01811000000",
           rep="Nadia", day=date(2026, 6, 4)),
])


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------
def test_an_agency_is_matched_across_how_the_name_was_typed():
    """The sheet says 'AB Travels', the rep wrote 'AB Travel'."""
    got = scl.contacts_from_visits(CONTACTS)
    rows = scl.build(_result(), got)
    ab = next(r for r in rows if r.movement.customer == "AB Travels")
    assert ab.contact.phone == "01711000000"
    assert ab.contact.contact == "Mr Rahman"


def test_a_trade_suffix_does_not_stop_a_match():
    rows = scl.build(_result(), scl.contacts_from_visits(CONTACTS))
    q = next(r for r in rows if r.movement.customer == "Quiet Tours")
    assert q.contact.phone == "01811000000"


def test_a_later_visit_wins_but_never_erases_a_number_with_a_blank():
    data = _Data([
        _Visit("AB Travel", contact="Mr Rahman", phone="01711000000",
               day=date(2026, 6, 1)),
        _Visit("AB Travel", contact="Mr Khan", phone="", day=date(2026, 8, 1)),
    ])
    got = scl.contacts_from_visits(data)["abtravel"]
    assert got.contact == "Mr Khan"           # newer name taken
    assert got.phone == "01711000000"         # older number kept
    assert got.last_seen == date(2026, 8, 1)


# --------------------------------------------------------------------------
# who is on the list
# --------------------------------------------------------------------------
def test_only_the_agencies_that_went_backwards_are_listed():
    names = [r.movement.customer
             for r in scl.build(_result(), scl.contacts_from_visits(CONTACTS))]
    assert "Growing" not in names
    assert set(names) == {"AB Travels", "Quiet Tours", "Small Dip"}


def test_the_list_is_ranked_by_money_lost():
    rows = scl.build(_result(), scl.contacts_from_visits(CONTACTS))
    assert [r.movement.customer for r in rows][0] == "AB Travels"
    assert rows[0].movement.change < rows[-1].movement.change


def test_an_agency_with_no_contact_is_kept_not_dropped():
    """An account losing real money that nobody has visited is the most
    important row on the sheet, not one to hide."""
    rows = scl.build(_result(), scl.contacts_from_visits(CONTACTS))
    dip = next(r for r in rows if r.movement.customer == "Small Dip")
    assert dip.reachable is False
    assert dip.contact.phone == ""


def test_it_works_with_no_visit_reports_at_all():
    rows = scl.build(_result(), None)
    assert len(rows) == 3
    assert all(r.reachable is False for r in rows)


# --------------------------------------------------------------------------
# what each row says
# --------------------------------------------------------------------------
def test_days_silent_counts_to_the_end_of_the_period():
    rows = scl.build(_result(), {})
    q = next(r for r in rows if r.movement.customer == "Quiet Tours")
    assert q.days_silent == (date(2026, 8, 31) - date(2026, 5, 2)).days


def test_the_reason_names_the_bucket_in_plain_words():
    rows = {r.movement.customer: r
            for r in scl.build(_result(), {})}
    assert rows["Quiet Tours"].why == "Stopped buying altogether"
    assert "Down 89%" in rows["AB Travels"].why


def test_a_refunder_is_described_as_one():
    rows = [_row("Refunder", 0, -200_000)] + \
        [_row("Refunder", i, 2_000_000) for i in (1, 2, 3)]
    got = scl.build(sm.build(rows, AUG), {})
    assert got[0].why == "Refunded more than they bought"


# --------------------------------------------------------------------------
# the workbook
# --------------------------------------------------------------------------
def _book(tmp_path, contacts=None):
    out = tmp_path / "calls.xlsx"
    scl.build_workbook(_result(), out,
                       contacts if contacts is not None
                       else scl.contacts_from_visits(CONTACTS))
    return load_workbook(out)


def _text(ws) -> str:
    return " ".join(str(c.value) for row in ws.iter_rows() for c in row
                    if c.value is not None)


def _column(ws, header, limit=40):
    for r in range(1, ws.max_row + 1):
        for c in range(1, 22):
            if ws.cell(row=r, column=c).value == header:
                return [ws.cell(row=i, column=c).value
                        for i in range(r + 1, min(r + 1 + limit,
                                                  ws.max_row + 1))]
    raise AssertionError(f"no column headed {header!r}")


def test_the_workbook_has_one_sheet_a_rep_can_work_from(tmp_path):
    wb = _book(tmp_path)
    assert wb.sheetnames == ["Call list"]
    assert _column(wb["Call list"], "Phone")[0] == "01711000000"


def test_the_workbook_leaves_blank_columns_for_the_rep_to_fill(tmp_path):
    ws = _book(tmp_path)["Call list"]
    for header in ("Called on", "Spoke to", "Outcome"):
        assert _column(ws, header)[0] is None


def test_the_header_says_how_many_can_actually_be_rung(tmp_path):
    said = _text(_book(tmp_path)["Call list"])
    assert "2 of 3 have someone to ring" in said.replace("\n", " ")


def test_an_empty_list_says_so_rather_than_printing_a_bare_grid(tmp_path):
    rows = [_row("Growing", 0, 9_000_000)] + \
        [_row("Growing", i, 1_000_000) for i in (1, 2, 3)]
    out = tmp_path / "empty.xlsx"
    scl.build_workbook(sm.build(rows, AUG), out, {})
    ws = load_workbook(out)["Call list"]
    assert "Nobody went backwards this period." in _text(ws)


def test_the_filename_names_the_period():
    assert scl.default_filename(AUG) == "Call_List_Aug2026.xlsx"
