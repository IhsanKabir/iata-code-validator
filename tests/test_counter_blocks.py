"""Enquiry log and timesheet blocks.

The rule these tests exist to hold: a blank is reported as a blank. Nothing here
may infer, average away, or quietly drop a value the staff did not write.
"""
import re

import pytest

from src import counter_blocks as cb

EMP_ID = re.compile(r"^USBA[-\s]?\d{4,6}$", re.I)


# --------------------------------------------------------------------------
# outcomes
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw, outcome", [
    ("YES", "converted"),
    ("Yes", "converted"),
    ("BOOKED", "converted"),
    ("SOLD", "converted"),
    ("NO", "lost"),
    ("N0", "lost"),                       # the typo appears 12 times
    ("NO(HIGH PRICE)", "lost"),
    ("NO (NO SEATS ARE AVAILABLE)", "lost"),
    ("FARE HIGH", "lost"),
    # "SOLD OUT" is the FLIGHT being full, so the sale was lost -- the lost
    # patterns are matched before the converted ones for exactly this reason
    ("SOLD OUT", "lost"),
    ("PENDING", "pending"),
    ("BOOKING", "pending"),
    ("", cb.NOT_WRITTEN),
    (None, cb.NOT_WRITTEN),
    ("   ", cb.NOT_WRITTEN),
    ("¯\\_(ツ)_/¯", cb.NOT_WRITTEN),      # unrecognised is NOT quietly a 'no'
])
def test_outcome_classification(raw, outcome):
    assert cb.classify_outcome(raw)[0] == outcome


def test_the_reason_text_is_kept_because_it_says_why_the_sale_was_lost():
    _, reason = cb.classify_outcome("NO (NO SEATS ARE AVAILABLE)")
    assert "SEATS" in reason.upper()
    _, reason = cb.classify_outcome("NO(HIGH PRICE)")
    assert "PRICE" in reason.upper()


def test_an_unrecognised_outcome_is_not_written_not_lost():
    """Counting an unreadable answer as a lost sale would invent failures."""
    outcome, reason = cb.classify_outcome("maybe later??")
    assert outcome == cb.NOT_WRITTEN
    assert reason == "maybe later??"


# --------------------------------------------------------------------------
# durations
# --------------------------------------------------------------------------
@pytest.mark.parametrize("cells, minutes", [
    (["10 MINUTES"], 10),
    (["05 MINUTES"], 5),
    (["5 MIN"], 5),
    (["1.5 HOURS"], 90),
    (["09 HOURS"], 540),
    (["UNABLE TO COUNT"], None),
    ([""], None),
    ([None], None),
    (["Others Work For 9 HR"], None),      # the block title, not a duration
])
def test_duration_parsing(cells, minutes):
    assert cb.parse_duration(cells) == minutes


def test_unable_to_count_is_never_treated_as_zero():
    """Averaging it as zero would read as an idle employee."""
    a = cb.ActivitySummary(times=[
        cb.TimeRow("X", 1, "USBA-90001", minutes=10),
        cb.TimeRow("X", 1, "USBA-90001", minutes=None),
    ])
    assert a.time_rows == 2
    assert a.timed_rows == 1
    assert a.minutes == 10
    per = a.minutes_by_employee()["USBA-90001"]
    assert per == {"minutes": 10, "rows": 2, "blank": 1}


def test_a_whole_shift_row_is_not_added_to_task_time():
    """One counter logs a single 09 HOURS line a day, another logs each job.
    Summing both gave 531 hours from 61 rows."""
    a = cb.ActivitySummary(times=[
        cb.TimeRow("X", 1, "USBA-90001", minutes=540),   # the whole shift
        cb.TimeRow("X", 1, "USBA-90001", minutes=15),
        cb.TimeRow("X", 1, "USBA-90001", minutes=20),
    ])
    assert a.minutes == 35
    assert a.shift_rows == 1


# --------------------------------------------------------------------------
# conversion only over what was written
# --------------------------------------------------------------------------
def test_conversion_ignores_rows_with_no_written_outcome():
    a = cb.ActivitySummary(queries=[
        cb.QueryRow("X", 1, outcome="converted"),
        cb.QueryRow("X", 1, outcome="lost"),
        cb.QueryRow("X", 1, outcome=cb.NOT_WRITTEN),
        cb.QueryRow("X", 1, outcome=cb.NOT_WRITTEN),
    ])
    assert a.query_total == 4
    assert a.judged == 2
    assert a.conversion == pytest.approx(0.5)   # NOT 1-in-4


def test_conversion_is_none_when_nothing_was_written():
    a = cb.ActivitySummary(queries=[cb.QueryRow("X", 1, outcome=cb.NOT_WRITTEN)])
    assert a.conversion is None


def test_pending_counts_as_neither_won_nor_lost():
    a = cb.ActivitySummary(queries=[
        cb.QueryRow("X", 1, outcome="converted"),
        cb.QueryRow("X", 1, outcome="pending"),
    ])
    assert a.conversion == pytest.approx(1.0)
    assert a.judged == 2


# --------------------------------------------------------------------------
# parsing a sheet
# --------------------------------------------------------------------------
QUERY_HEADER = ["", "Query Method", "Query Type", "", "", "", "", "", "",
                "Segment", "", "Did query convert to sale?", "", "",
                "Mobile Customer if applicable", ""]
TIME_HEADER = ["Employee ID", "Employee Name", "Shift name", "File Management",
               "Nature of query by customer", "Agency Name", "Others Query",
               "Total Time Spent on", "Remarks"]


def test_the_enquiry_block_is_read_with_its_blanks():
    grid = [
        QUERY_HEADER,
        ["", "Walk-in", "TICKET PRICE", "", "", "", "", "", "", "DAC-CXB", "",
         "YES", "", "", "01700000001", ""],
        ["", "", "REISSUE", "", "", "", "", "", "", "DAC-JED", "",
         "NO(HIGH PRICE)", "", "", "01700000002", ""],
        ["", "", "TICKET PRICE", "", "", "", "", "", "", "DAC-DXB", "",
         "", "", "", "01700000003", ""],
    ]
    a = cb.parse_activity(grid, "Test", 6, EMP_ID)
    assert a.query_total == 3
    assert a.outcomes == {"converted": 1, "lost": 1, cb.NOT_WRITTEN: 1}
    # the method carries down the block the way the sheet lays it out
    assert [q.method for q in a.queries] == ["WALK-IN"] * 3
    assert a.queries[1].segment == "DAC-JED"
    assert a.conversion == pytest.approx(0.5)


def test_the_timesheet_block_is_read_with_its_blanks():
    grid = [
        TIME_HEADER,
        ["USBA-90001", "ALEX ROY", "COUNTER-M", "ISSUED TICKET", "", "", "",
         "10 MINUTES", ""],
        ["USBA-90001", "ALEX ROY", "COUNTER-M", "WALK IN PAX SERVICE", "", "", "",
         "UNABLE TO COUNT", ""],
        ["USBA-90002", "SAM LEE", "COUNTER-G", "REISSUED & FILE", "", "", "",
         "15 MINUTES", ""],
    ]
    a = cb.parse_activity(grid, "Test", 6, EMP_ID)
    assert a.time_rows == 3
    assert a.timed_rows == 2
    assert a.minutes == 25
    assert a.minutes_by_employee()["USBA-90001"]["blank"] == 1


def test_the_two_blocks_do_not_bleed_into_each_other():
    grid = [
        QUERY_HEADER,
        ["", "Phone Call", "TICKET PRICE", "", "", "", "", "", "", "DAC-CXB", "",
         "YES", "", "", "", ""],
        ["Sky Star Customer"],
        ["FFP NO.", "Contact Number"],
        ["11419605", "01700000009"],
        TIME_HEADER,
        ["USBA-90001", "ALEX ROY", "COUNTER-M", "ISSUED TICKET", "", "", "",
         "10 MINUTES", ""],
    ]
    a = cb.parse_activity(grid, "Test", 6, EMP_ID)
    assert a.query_total == 1          # the FFP row is not an enquiry
    assert a.time_rows == 1


def test_by_counter_reports_blanks_alongside_counts():
    a = cb.ActivitySummary(
        queries=[cb.QueryRow("A", 1, outcome="converted", received_by="ALEX"),
                 cb.QueryRow("A", 1, outcome=cb.NOT_WRITTEN)],
        times=[cb.TimeRow("A", 1, "USBA-90001", minutes=None)])
    v = a.by_counter()["A"]
    assert v["queries"] == 2
    assert v["converted"] == 1
    assert v[cb.NOT_WRITTEN] == 1
    assert v["attributed"] == 1
    assert v["time_blank"] == 1


# --------------------------------------------------------------------------
# naming staff behind an unreported PNR
# --------------------------------------------------------------------------
class _Finding:
    def __init__(self, locator, customer="", note=""):
        self.locator = locator
        self.customer = customer
        self.note = note


class _Details:
    def __init__(self, **kw):
        self.customer_name = kw.get("customer_name", "")
        self.phone = kw.get("phone", "")
        self.pnr_status = kw.get("pnr_status", "")
        self.pax_count = kw.get("pax_count", 0)
        self.booked_route = kw.get("booked_route", "")


def test_a_looked_up_pnr_fills_in_what_the_counter_never_wrote():
    findings = [_Finding("0A1111")]
    calls = []

    def lookup(_session, code):
        calls.append(code)
        return _Details(customer_name="SOME AGENCY", pnr_status="Issued",
                        booked_route="DAC-CXB-DAC")

    cb.enrich_from_zenith(None, findings, lookup=lookup)
    assert calls == ["0A1111"]
    assert findings[0].customer == "SOME AGENCY"
    assert "Issued" in findings[0].note
    assert "DAC-CXB-DAC" in findings[0].note


def test_each_pnr_is_looked_up_once_however_many_findings_share_it():
    findings = [_Finding("0A1111"), _Finding("0A1111"), _Finding("0A2222")]
    calls = []
    cb.enrich_from_zenith(None, findings,
                          lookup=lambda _s, c: calls.append(c) or _Details())
    assert calls == ["0A1111", "0A2222"]


def test_a_failed_lookup_is_recorded_as_unknown_not_as_nobody():
    findings = [_Finding("0A1111")]

    def boom(_session, _code):
        raise TimeoutError("no answer")

    seen = cb.enrich_from_zenith(None, findings, lookup=boom)
    assert "lookup failed" in seen["0A1111"]["status"]
    assert findings[0].customer == ""          # nothing invented


def test_a_pnr_the_system_cannot_find_says_so():
    findings = [_Finding("0A1111")]
    seen = cb.enrich_from_zenith(None, findings, lookup=lambda _s, _c: None)
    assert seen["0A1111"]["status"] == "not found"


def test_an_existing_customer_name_is_not_overwritten():
    findings = [_Finding("0A1111", customer="FROM THE SALES DATA")]
    cb.enrich_from_zenith(None, findings,
                          lookup=lambda _s, _c: _Details(customer_name="OTHER"))
    assert findings[0].customer == "FROM THE SALES DATA"


def test_the_lookup_stops_when_asked():
    findings = [_Finding(f"0A{i:04d}") for i in range(10)]
    calls = []
    cb.enrich_from_zenith(
        None, findings, lookup=lambda _s, c: calls.append(c) or _Details(),
        stop_flag=lambda: len(calls) >= 3)
    assert len(calls) == 3


def test_the_lookup_is_capped_so_a_huge_gap_cannot_hammer_zenith():
    findings = [_Finding(f"0A{i:04d}") for i in range(50)]
    calls = []
    cb.enrich_from_zenith(None, findings, max_lookups=5,
                          lookup=lambda _s, c: calls.append(c) or _Details())
    assert len(calls) == 5
