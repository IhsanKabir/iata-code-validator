"""Text the reps repeated.

Fixtures are synthetic, shaped on what the 1-10 September 2026 reports did:
one rep giving 53 agencies one sentence, one phone number on three agencies,
one agency spelled two ways under one number.
"""
from datetime import date

import pytest
from openpyxl import load_workbook

from src import visit_master as vm
from src import visit_repeats as vr

LINE = "Motivate to increase sale from BS ID, talking about domestic routes"


def visit(rep, agency, remarks="", *, day=1, phone="", contact=""):
    return vm.Visit(rep=rep, sheet=rep, day=date(2026, 9, day), zone="",
                    agency=agency, remarks=remarks, phone=phone,
                    contact=contact)


def agencies(n):
    return [f"Agency {chr(65 + i)} Travels" for i in range(n)]


# --------------------------------------------------------------------------
# 1. one remark, many agencies
# --------------------------------------------------------------------------
def test_one_remark_on_five_agencies_is_a_stock_remark():
    vs = [visit("Hamed", a, LINE) for a in agencies(5)]
    res = vr.find_repeats(vs)
    assert [(s.rep, s.agencies, s.rows) for s in res.stock] == [("Hamed", 5, 5)]
    assert res.stock[0].share == 1.0


def test_four_agencies_is_below_the_line():
    assert vr.find_repeats([visit("Hamed", a, LINE)
                            for a in agencies(4)]).stock == []


def test_revisiting_one_agency_is_not_a_stock_remark():
    """Five visits to one agency with one remark is one conversation, ongoing."""
    vs = [visit("Hamed", "Agency A Travels", LINE, day=d) for d in range(1, 6)]
    assert vr.find_repeats(vs).stock == []


def test_a_changed_word_does_not_hide_a_stock_remark():
    vs = [visit("Robin", a, "I have pressured him to sell more tickets for "
                            "our DAC-SPD-DAC flights") for a in agencies(3)]
    vs += [visit("Robin", a, "I have pressured them to sell more tickets for "
                             "our DAC SPD DAC flights") for a in agencies(6)[3:]]
    res = vr.find_repeats(vs)
    assert len(res.stock) == 1
    assert res.stock[0].agencies == 6 and res.stock[0].wordings == 2


def test_short_stock_phrases_count_for_one_rep():
    """'Happy with our service' on 31 agencies says nothing about 31 visits."""
    vs = [visit("Rupom", a, "Happy with our service") for a in agencies(6)]
    assert vr.find_repeats(vs).stock[0].agencies == 6


# --------------------------------------------------------------------------
# 2. one remark, more than one rep
# --------------------------------------------------------------------------
def test_a_remark_copied_to_another_rep_is_listed():
    vs = [visit("Karim", "Alpha Travels", LINE),
          visit("Rahim", "Beta Tours", LINE.replace("domestic", "domestic and"))]
    res = vr.find_repeats(vs)
    assert len(res.shared) == 1
    assert res.shared[0].reps == ("Karim", "Rahim")
    assert not res.shared[0].joint and res.copied == 1


def test_two_reps_at_one_agency_on_one_day_is_a_joint_visit():
    vs = [visit("Karim", "Alpha Travels", LINE),
          visit("Rahim", "ALPHA TRAVEL", LINE)]
    res = vr.find_repeats(vs)
    assert res.shared[0].joint and res.copied == 0


def test_two_reps_saying_new_agency_is_not_copying():
    vs = [visit("Karim", "Alpha Travels", "New agency"),
          visit("Rahim", "Beta Tours", "New agency")]
    assert vr.find_repeats(vs).shared == []


def test_different_remarks_are_not_matched():
    vs = [visit("Karim", "Alpha Travels", LINE),
          visit("Rahim", "Beta Tours", "Asked for the Kuala Lumpur group fare "
                                       "and the new Singapore schedule")]
    assert vr.find_repeats(vs).shared == []


# --------------------------------------------------------------------------
# 3. one phone number, more than one agency
# --------------------------------------------------------------------------
def test_one_number_on_two_agencies_is_listed():
    vs = [visit("Atik", "Air Top", phone="01715612300"),
          visit("Atik", "DBM Travels & Tours", phone="01715-612300"),
          visit("Yeachir", "Grace Travels", phone="+880 1715 612300")]
    res = vr.find_repeats(vs)
    assert [(p.phone, len(p.agencies)) for p in res.phones] \
        == [("1715612300", 3)]


@pytest.mark.parametrize("a, b", [
    ("Darvish Travels & Tours", "DARWISH TRAVEL & TOURS"),   # spelled twice
    ("Ispahani", "Ispahani Group"),                         # words added
    ("Rajdhani Travels", "Rajdhani Travels And Typing"),
])
def test_one_agency_named_two_ways_is_not_two_agencies(a, b):
    vs = [visit("Fahad", a, phone="01912841956"),
          visit("Kanjul", b, phone="01912841956")]
    assert vr.find_repeats(vs).phones == []


def test_a_short_name_inside_a_longer_one_is_still_two_agencies():
    """'AB' inside 'ABC Travels' is a coincidence of letters, not one firm."""
    vs = [visit("Fahad", "AB", phone="01912841956"),
          visit("Kanjul", "ABC Travels", phone="01912841956")]
    assert len(vr.find_repeats(vs).phones) == 1


def test_a_different_person_at_each_agency_is_listed_first():
    vs = [visit("Atik", "Fly Feni", phone="01711531548", contact="Mr. Shaheen"),
          visit("Atik", "Universal Overseas", phone="01711531548",
                contact="Mr Shaheen"),
          visit("Atik", "Air Top", phone="01715612300", contact="Mr. Mamun"),
          visit("Atik", "DBM Travels", phone="01715612300",
                contact="Mr. Tariqul")]
    res = vr.find_repeats(vs)
    assert [(p.phone, p.same_contact) for p in res.phones] == [
        ("1715612300", False), ("1711531548", True)]
    assert res.phones_different == 1


@pytest.mark.parametrize("raw, keys", [
    ("01711654417", ["1711654417"]),
    ("01711-654417 / 01812345678", ["1711654417", "1812345678"]),
    ("2026-05-07 00:00:00", []),          # a date in the phone column
    ("07/05/2026", []),
    ("0000000000", []),                   # a placeholder
    ("123", []),
    ("", []),
])
def test_phone_numbers_are_read_and_dates_are_not(raw, keys):
    assert vr.phone_keys(raw) == keys


# --------------------------------------------------------------------------
# 4. one agency, more than one rep
# --------------------------------------------------------------------------
def test_an_agency_two_reps_visited_is_listed():
    vs = [visit("Farhad", "Fayaz Aviation", day=10),
          visit("Sabbit", "Fayaz Aviation", day=3),
          visit("Sabbit", "Other Travels")]
    res = vr.find_repeats(vs)
    assert [(o.agency, o.reps, o.visits) for o in res.overlaps] \
        == [("Fayaz Aviation", ("Farhad", "Sabbit"), 2)]


# --------------------------------------------------------------------------
# grouping, and the sheet
# --------------------------------------------------------------------------
def test_grouping_does_not_chain_small_edits_into_one_remark():
    """a~b is 90 and b~c is 86, but a~c is 75: chained, a Kuala Lumpur
    remark and a Bangkok one would be called the same remark."""
    a = "discussed the dhaka kuala lumpur fare with the owner today"
    b = "discussed the dhaka kuala lumpur fare with the manager yesterday"
    c = "discussed the dhaka bangkok fare with the manager yesterday"
    got = vr.cluster([a, b, c])
    assert got[b] == a
    assert got[c] == c


def test_the_findings_land_on_the_visits_sheet(tmp_path):
    """One sheet, as the rest of the summary: a sheet of its own goes unread."""
    from tests.test_visit_master import _report

    rows = [[a, "Mr. X", "Owner", f"0171000000{i}", "Motijheel",
             "BDT- 1000000/-", "KSA", LINE] for i, a in enumerate(agencies(5))]
    p = tmp_path / "visits.xlsx"
    _report(p, {"Hamed": [("11/09/2026", "Zone # 1", "Hamed", rows)]})
    out = tmp_path / "master.xlsx"
    res = vm.build_master(p, out, month=9, year=2026)
    assert res.stock_reps == 1
    book = load_workbook(out)
    assert book.sheetnames == ["Visits"]
    ws = book["Visits"]
    text = " ".join(str(c.value) for row in ws.iter_rows() for c in row
                    if c.value is not None)
    assert "REPEATED ENTRIES" in text and "Motivate to increase sale" in text
    # the scorecard says it too, on the rep's own line
    head = next(row for row in ws.iter_rows()
                if row[0].value == "Sales rep" and row[13].value)
    assert head[13].value == "Stock remark %"
    hamed = next(row for row in ws.iter_rows(min_row=head[0].row)
                 if row[0].value == "Hamed")
    assert hamed[13].value == 1.0
