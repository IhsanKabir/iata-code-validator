"""Reports sent again under another date or another rep's name.

Fixtures are synthetic, shaped on what the reports did: in 1-10 September
one rep's 3 Sep report was his 1 Sep report plus two new rows; in July one
rep filed the same five 9 Jul rows again on 16, 23 and 28 Jul.
"""
from datetime import date

from openpyxl import load_workbook

from src import visit_master as vm
from src import visit_repeats as vr
from src import visit_resends as vs

REMARKS = [
    "Discuss about Cox's Bazar Flight and the new evening schedule",
    "Discussed OTA commission rate and the cash back offer",
    "Suggested us morning flight should be on 10am to 11am",
    "Suggested us to operate a flight between 12pm to 3pm",
    "Suggested us to operate a weekly direct flight of RJH CXB RJH",
    "Suggested us to provide flexible fare for the group",
    "They said maximum pax want a starting fare from 4,500",
    "Discuss about how to increase morning flight load",
    "Asked for a refund on two tickets issued last week",
]
AGENCIES = [f"Agency {chr(65 + i)} Travels" for i in range(len(REMARKS))]


def visit(rep, agency, remarks, day, *, phone="", contact=""):
    return vm.Visit(rep=rep, sheet=rep, day=date(2026, 9, day), zone="",
                    agency=agency, remarks=remarks, phone=phone,
                    contact=contact)


def report(rep, day, n=len(REMARKS), start=0):
    return [visit(rep, a, t, day)
            for a, t in list(zip(AGENCIES, REMARKS))[start:start + n]]


def fresh(rep, day, n):
    return [visit(rep, f"New {day}-{i} Travels",
                  f"First visit on day {day}, talked about the agency {i} sales",
                  day) for i in range(n)]


def find(visits):
    visits = list(visits)
    return vs.find_resends(visits, vr.find_repeats(visits))


# --------------------------------------------------------------------------
# what counts
# --------------------------------------------------------------------------
def test_a_report_sent_again_under_a_later_date_is_whole():
    """3 Sep was 1 Sep with two new rows: nine of eleven repeat."""
    got = find(report("Barru", 1) + report("Barru", 3) + fresh("Barru", 3, 2))
    assert [(x.rep, x.day.day, x.source_day.day, len(x.copied), x.rows)
            for x in got] == [("Barru", 3, 1, 9, 11)]
    assert got[0].whole and got[0].same_rep
    assert vs.resend_counts(got) == {"Barru": (1, 9)}


def test_part_of_a_report_is_listed_but_not_whole():
    got = find(report("Nazmul", 1) + report("Nazmul", 4, n=3)
               + fresh("Nazmul", 4, 7))
    assert [(len(x.copied), x.rows, x.whole) for x in got] == [(3, 10, False)]
    assert vs.resend_counts(got) == {"Nazmul": (0, 3)}


def test_two_matching_rows_are_two_days_that_happen_to_agree():
    assert find(report("Nazmul", 1) + report("Nazmul", 4, n=2)
                + fresh("Nazmul", 4, 5)) == []


def test_a_revisit_with_a_new_conversation_is_not_a_copy():
    """Same agencies, something new said: that is a route, worked."""
    again = [visit("Barru", a, f"Followed up on {a}, they booked two Umrah "
                               f"groups", 3) for a in AGENCIES]
    assert find(report("Barru", 1) + again) == []


def test_short_remarks_are_not_compared():
    """'Regular visit' at the same agency every week is a route, not a copy."""
    vs_ = [visit("Robin", a, "Regular visit", d)
           for d in (1, 8) for a in AGENCIES[:4]]
    assert find(vs_) == []


def test_a_stock_remark_is_left_to_repeated_entries():
    """A rep who writes one line everywhere matches on every revisit; that
    is already the Stock remark % column, and is not called a re-send."""
    line = "Motivate to increase sale from BS ID, talking about domestic routes"
    vs_ = [visit("Hamed", a, line, d) for d in (1, 5) for a in AGENCIES]
    assert find(vs_) == []


def test_a_copy_with_a_typo_fixed_still_matches():
    later = [visit("Barru", a, t.replace("Suggested", "Sugested"), 3)
             for a, t in zip(AGENCIES, REMARKS)]
    got = find(report("Barru", 1) + later)
    assert got and got[0].whole


def test_undated_rows_are_not_compared():
    undated = [vm.Visit(rep="Barru", sheet="Barru", day=None, zone="",
                        agency=a, remarks=t) for a, t in zip(AGENCIES, REMARKS)]
    assert find(report("Barru", 1) + undated) == []


# --------------------------------------------------------------------------
# which report it repeats
# --------------------------------------------------------------------------
def test_one_block_on_three_later_dates_is_one_finding_with_three_dates():
    vs_ = report("Fahad", 9) + fresh("Fahad", 9, 5)
    for d in (16, 23, 28):
        vs_ += report("Fahad", d, n=5) + fresh("Fahad", d, 5)
    got = find(vs_)
    # 23 Jul is reported against 9 Jul, its first sending, not against 16 Jul
    assert {x.source_day.day for x in got} == {9}
    blocks = vs.group_resends(got)
    assert len(blocks) == 1
    assert [b.day.day for b in blocks[0]] == [16, 23, 28]


def test_rows_from_two_earlier_reports_are_both_named():
    vs_ = (report("Barru", 1, n=4) + fresh("Barru", 1, 4)
           + report("Barru", 2, n=4, start=4) + fresh("Barru", 2, 4)
           + report("Barru", 5, n=8))
    got = find(vs_)
    assert sorted((x.source_day.day, len(x.copied)) for x in got) \
        == [(1, 4), (2, 4)]
    # neither earlier report alone is 80% of 5 Sep, but every row is copied:
    # it is a day sent again all the same
    assert not any(x.whole for x in got)
    assert vs.resend_counts(got) == {"Barru": (1, 8)}


def test_another_reps_report_under_a_later_date_is_named():
    got = find(report("Karim", 1) + report("Sabbit", 4))
    assert [(x.rep, x.source_rep, x.whole, x.same_rep) for x in got] \
        == [("Sabbit", "Karim", True, False)]


def test_two_reps_on_one_day_is_a_joint_visit_and_not_counted():
    got = find(report("Karim", 2) + report("Sabbit", 2))
    assert len(got) == 1 and got[0].same_day
    assert vs.resend_counts(got) == {}


def test_the_rows_keep_the_order_the_rep_wrote_them():
    later = list(reversed(report("Barru", 3)))
    got = find(report("Barru", 1) + later)
    assert [e.agency for e in got[0].copied] == [v.agency for v in later]


def test_two_identical_looking_rows_count_twice():
    """Two contacts at one agency, one remark: two rows copied, not one."""
    two = [visit("Barru", "Uni Travels", REMARKS[0], d, contact=c)
           for d in (1, 3) for c in ("Kamal", "Abhijeet")]
    got = find(two + report("Barru", 1, n=2, start=1)
               + report("Barru", 3, n=2, start=1))
    assert vs.resend_counts(got) == {"Barru": (1, 4)}


# --------------------------------------------------------------------------
# the sheet
# --------------------------------------------------------------------------
def test_the_finding_lands_under_the_scorecard(tmp_path):
    from tests.test_visit_master import _report

    def rows(day, n, start=0):
        return [[a, "Mr. X", "Owner", f"0171000000{i}", "Motijheel", "", "",
                 t] for i, (a, t) in enumerate(
                     list(zip(AGENCIES, REMARKS))[start:start + n])]

    p = tmp_path / "visits.xlsx"
    _report(p, {"Barru": [("01/09/2026", "Zone # 1", "Barru", rows(1, 9)),
                          ("03/09/2026", "Zone # 1", "Barru", rows(3, 9))]})
    out = tmp_path / "master.xlsx"
    res = vm.build_master(p, out, month=9, year=2026)
    assert (res.resent_reports, res.resent_rows) == (1, 9)
    book = load_workbook(out)
    assert book.sheetnames == ["Visits"]
    ws = book["Visits"]
    head = next(row for row in ws.iter_rows()
                if row[0].value == "Sales rep" and row[14].value)
    assert (head[14].value, head[15].value) == ("Days re-sent", "Rows re-sent")
    barru = next(row for row in ws.iter_rows(min_row=head[0].row)
                 if row[0].value == "Barru")
    assert (barru[14].value, barru[15].value) == (1, 9)
    assert barru[20].value == "REPORT RE-SENT"
    joined = "\n".join(str(c.value) for row in ws.iter_rows() for c in row
                       if c.value is not None)
    assert "WHOLE REPORT SENT AGAIN:  03 Sep (9 of 9, 100%)" in joined
    assert (joined.index("SALES REP SCORECARD")
            < joined.index("RE-SENT REPORTS")
            < joined.index("REPEATED ENTRIES"))
    assert all(t in joined for t in REMARKS)


def test_no_findings_says_so(tmp_path):
    from tests.test_visit_master import _report

    p = tmp_path / "visits.xlsx"
    _report(p, {"Barru": [("01/09/2026", "Zone # 1", "Barru",
                           [["Agency A Travels", "Mr. X", "Owner", "01710000001",
                             "Motijheel", "", "", REMARKS[0]]])]})
    out = tmp_path / "master.xlsx"
    res = vm.build_master(p, out, month=9, year=2026)
    assert (res.resent_reports, res.resent_rows) == (0, 0)
    text = [str(c.value) for row in load_workbook(out)["Visits"].iter_rows()
            for c in row if c.value is not None]
    assert "No day's report repeats another one." in text
