"""The airline schedule, drawn from rows a Flight Loads pull already returned.

Every rule pinned here was measured on a real 2,528-row export, and each one
produces a wrong schedule if ignored.
"""
from datetime import date

from openpyxl import load_workbook

from src import airline_schedule as asch


class _Row:
    """Stands in for zenith_client.FlightLoadRow."""

    def __init__(self, flight, fdate, leg, rng, *, dep_time="12:30",
                 aircraft="Boeing 737-800", registration="S2-AJE",
                 status="AS-Flight open", seats="13/410 97%",
                 cabin="Economy", dow="Tue"):
        self.flight_number, self.flight_date = flight, fdate
        self.leg_route = leg
        self.leg_origin, self.leg_destination = leg.split("-")
        self.leg_local_time_range = rng
        self.departure_time = dep_time          # per FLIGHT, repeats across legs
        self.aircraft, self.registration = aircraft, registration
        self.inventory_status, self.seats_available = status, seats
        self.leg_cabin, self.day_of_week = cabin, dow


# --- leg times come from the leg, never from the flight -------------------

def test_leg_order_follows_the_legs_own_departure_not_the_flights():
    """Real case: BS344 on 01/09 reports 12:30 against BOTH legs, while the
    legs actually ran 12:30-19:50 then 20:40-21:35. Ordering on the flight's
    single departure time cannot order anything."""
    rows = [
        _Row("BS344", "01/09/2026", "CGP-DAC", "01/09/2026 20:40 - 21:35"),
        _Row("BS344", "01/09/2026", "DXB-CGP", "01/09/2026 12:30 - 19:50"),
    ]
    res = asch.build(rows)
    order = {l.route: l.order for l in res.legs}
    assert order == {"DXB-CGP": 1, "CGP-DAC": 2}


def test_the_leg_range_supplies_departure_and_arrival():
    res = asch.build([_Row("BS341", "01/09/2026", "DAC-CGP",
                           "01/09/2026 05:00 - 07:00")])
    (leg,) = res.legs
    assert (leg.dep, leg.arr) == ("05:00", "07:00")
    assert leg.leg_date == date(2026, 9, 1)


# --- 235 real legs land before they leave, by the clock -------------------

def test_a_leg_landing_before_it_left_is_marked_next_day():
    res = asch.build([_Row("BS382", "01/09/2026", "RUH-DAC",
                           "01/09/2026 19:10 - 04:20")])
    (leg,) = res.legs
    assert leg.next_day
    assert leg.elapsed == "9:10"        # not a negative duration


def test_a_same_day_leg_is_not_marked_next_day():
    res = asch.build([_Row("BS341", "01/09/2026", "DAC-CGP",
                           "01/09/2026 05:00 - 07:00")])
    assert not res.legs[0].next_day
    assert res.legs[0].elapsed == "2:00"


# --- 13 real legs operate on a later date than they are filed under ------

def test_a_leg_operating_on_another_date_is_reported_not_smoothed_over():
    """BS350 filed under 03/09 actually operates 04/09. The leg's own date
    wins, and the discrepancy is stated."""
    res = asch.build([_Row("BS350", "03/09/2026", "CXB-DAC",
                           "04/09/2026 06:30 - 07:25")])
    (leg,) = res.legs
    assert leg.leg_date == date(2026, 9, 4)
    assert leg.flight_date == "03/09/2026"
    assert leg.date_differs
    assert res.filed_under_another_date == 1


def test_a_leg_on_its_filed_date_is_not_flagged():
    res = asch.build([_Row("BS341", "01/09/2026", "DAC-CGP",
                           "01/09/2026 05:00 - 07:00")])
    assert not res.legs[0].date_differs
    assert res.filed_under_another_date == 0


# --- rows are per cabin; a schedule is per leg ---------------------------

def test_cabins_collapse_into_one_leg_with_their_seats_summed():
    rows = [
        _Row("BS101", "01/09/2026", "DAC-CGP", "01/09/2026 07:00 - 07:55",
             seats="2/72 97%"),
        _Row("BS101", "01/09/2026", "DAC-CGP", "01/09/2026 07:00 - 07:55",
             seats="0/8 100%", cabin="Business"),
    ]
    res = asch.build(rows)
    assert len(res.legs) == 1
    (leg,) = res.legs
    assert leg.cabins == 2
    assert leg.seats == 80              # 72 + 8, not counted twice


def test_two_different_legs_stay_two_legs():
    rows = [
        _Row("BS349", "02/09/2026", "DAC-CGP", "02/09/2026 16:25 - 17:20"),
        _Row("BS349", "02/09/2026", "CGP-AUH", "02/09/2026 18:10 - 21:10"),
    ]
    res = asch.build(rows)
    assert len(res.legs) == 2
    assert res.multi_leg == 1


# --- blanks are never presented as facts --------------------------------

def test_a_blank_registration_or_status_reads_as_not_written():
    """27 real rows carry no registration and 74 no inventory status."""
    res = asch.build([_Row("BS341", "01/09/2026", "DAC-CGP",
                           "01/09/2026 05:00 - 07:00",
                           registration="", status=None)])
    (leg,) = res.legs
    assert leg.registration == asch.NOT_WRITTEN
    assert leg.inventory_status == asch.NOT_WRITTEN


def test_an_unreadable_time_range_is_counted_never_invented():
    res = asch.build([_Row("BS341", "01/09/2026", "DAC-CGP", "sometime")])
    (leg,) = res.legs
    assert leg.leg_date is None
    assert (leg.dep, leg.arr) == ("", "")
    assert res.unreadable_ranges == 1


def test_an_unreadable_capacity_leaves_seats_unset_rather_than_zero():
    res = asch.build([_Row("BS341", "01/09/2026", "DAC-CGP",
                           "01/09/2026 05:00 - 07:00", seats="n/a")])
    assert res.legs[0].seats is None


# --- sector ---------------------------------------------------------------

def test_sector_is_domestic_only_when_both_ends_are():
    dom = asch.build([_Row("BS101", "01/09/2026", "DAC-CGP",
                           "01/09/2026 07:00 - 07:55")]).legs[0]
    intl = asch.build([_Row("BS382", "01/09/2026", "RUH-DAC",
                            "01/09/2026 19:10 - 04:20")]).legs[0]
    assert dom.sector == "Domestic"
    assert intl.sector == "International"


# --- the workbook ---------------------------------------------------------

def test_the_workbook_states_the_period_that_was_searched(tmp_path):
    out = tmp_path / "sched.xlsx"
    asch.write_airline_schedule(
        out, [_Row("BS341", "01/09/2026", "DAC-CGP",
                   "01/09/2026 05:00 - 07:00")],
        date_from="01/09/2026", date_to="06/09/2026")
    wb = load_workbook(out, data_only=True)
    assert wb.sheetnames == ["Airline Schedule", "By route"]
    text = " ".join(str(c.value) for row in wb["Airline Schedule"].iter_rows()
                    for c in row if c.value)
    assert "01/09/2026 to 06/09/2026" in text
    assert "nothing was fetched again" in text


def test_the_sheet_refuses_to_call_a_clock_gap_block_time(tmp_path):
    """Departure and arrival are local clocks at two airports, so the gap is
    only a duration when both ends share a timezone."""
    out = tmp_path / "sched.xlsx"
    asch.write_airline_schedule(
        out, [_Row("BS382", "01/09/2026", "RUH-DAC",
                   "01/09/2026 19:10 - 04:20")])
    ws = load_workbook(out, data_only=True)["Airline Schedule"]
    text = " ".join(str(c.value) for row in ws.iter_rows() for c in row
                    if c.value)
    assert "Elapsed on local clocks" in text
    assert "NOT block time" in text
    assert "block time" not in text.replace("NOT block time", "")


def test_the_by_route_sheet_counts_departures_per_route(tmp_path):
    rows = [
        _Row("BS341", f"0{d}/09/2026", "DAC-CGP",
             f"0{d}/09/2026 05:00 - 07:00") for d in (1, 2, 3)
    ] + [_Row("BS382", "01/09/2026", "RUH-DAC", "01/09/2026 19:10 - 04:20")]
    out = tmp_path / "sched.xlsx"
    asch.write_airline_schedule(out, rows)
    ws = load_workbook(out, data_only=True)["By route"]
    body = [[c.value for c in r] for r in ws.iter_rows()]
    top = next(r for r in body if r and r[0] == "DAC-CGP")
    assert top[2] == 3                 # three departures
    assert top[3] == 3                 # across three days


def test_an_empty_pull_still_writes_a_readable_workbook(tmp_path):
    out = tmp_path / "sched.xlsx"
    asch.write_airline_schedule(out, [])
    wb = load_workbook(out, data_only=True)
    assert "Airline Schedule" in wb.sheetnames


def test_building_is_deterministic():
    rows = [
        _Row("BS349", "02/09/2026", "CGP-AUH", "02/09/2026 18:10 - 21:10"),
        _Row("BS349", "02/09/2026", "DAC-CGP", "02/09/2026 16:25 - 17:20"),
    ]
    shape = lambda r: [(l.flight_number, l.route, l.order, l.dep)
                       for l in r.legs]
    assert shape(asch.build(rows)) == shape(asch.build(rows))


# --- the heading must never name a period the legs do not cover ----------

def test_the_heading_states_the_period_the_legs_actually_cover(tmp_path):
    """These very rows were once labelled '01/09 to 06/09' while carrying all
    30 dates of September. A heading that names a period it does not contain
    is worse than no heading."""
    rows = [_Row("BS341", f"{d:02d}/09/2026", "DAC-CGP",
                 f"{d:02d}/09/2026 05:00 - 07:00") for d in (1, 15, 30)]
    out = tmp_path / "s.xlsx"
    res = asch.write_airline_schedule(out, rows, date_from="01/09/2026",
                                      date_to="06/09/2026")
    assert res.covered_span == "01 Sep 2026 to 30 Sep 2026"
    assert res.dates_covered == 3
    assert res.range_disagrees
    text = " ".join(str(c.value) for row in
                    load_workbook(out, data_only=True)["Airline Schedule"]
                    .iter_rows() for c in row if c.value)
    assert "01 Sep 2026 to 30 Sep 2026" in text
    assert "CHECK THE RANGE" in text
    assert "which these legs do not sit inside" in text


def test_a_matching_range_is_not_flagged(tmp_path):
    rows = [_Row("BS341", "02/09/2026", "DAC-CGP",
                 "02/09/2026 05:00 - 07:00")]
    out = tmp_path / "s.xlsx"
    res = asch.write_airline_schedule(out, rows, date_from="01/09/2026",
                                      date_to="06/09/2026")
    assert not res.range_disagrees
    text = " ".join(str(c.value) for row in
                    load_workbook(out, data_only=True)["Airline Schedule"]
                    .iter_rows() for c in row if c.value)
    assert "CHECK THE RANGE" not in text
    assert "Searched 01/09/2026 to 06/09/2026" in text


def test_one_day_of_legs_reads_as_a_single_date():
    res = asch.build([_Row("BS341", "01/09/2026", "DAC-CGP",
                           "01/09/2026 05:00 - 07:00")])
    assert res.covered_span == "01 Sep 2026"


def test_no_dated_legs_says_so_rather_than_inventing_a_span():
    res = asch.build([_Row("BS341", "01/09/2026", "DAC-CGP", "sometime")])
    assert res.covered_span == "no dated legs"
    assert res.first_date is None and res.dates_covered == 0


# --- the remaining latent traps -----------------------------------------

def test_the_same_cabin_twice_cannot_inflate_the_aircraft():
    """No duplicate cabin row appeared in the 2,528-row export, but summing
    one twice would report an aircraft bigger than it is."""
    row = _Row("BS101", "01/09/2026", "DAC-CGP", "01/09/2026 07:00 - 07:55",
               seats="2/72 97%")
    res = asch.build([row, row])
    (leg,) = res.legs
    assert leg.cabins == 1
    assert leg.seats == 72          # not 144


def test_different_cabins_still_sum():
    res = asch.build([
        _Row("BS101", "01/09/2026", "DAC-CGP", "01/09/2026 07:00 - 07:55",
             seats="2/72 97%"),
        _Row("BS101", "01/09/2026", "DAC-CGP", "01/09/2026 07:00 - 07:55",
             seats="0/8 100%", cabin="Business"),
    ])
    assert res.legs[0].seats == 80


def test_a_leg_with_no_readable_time_sorts_last_not_first():
    """Treating an unreadable range as 00:00 put it at the top of the sheet,
    where it read as the day's first departure."""
    res = asch.build([
        _Row("BS999", "01/09/2026", "DAC-ZYL", "not a time"),
        _Row("BS341", "01/09/2026", "DAC-CGP", "01/09/2026 05:00 - 07:00"),
    ])
    assert [l.flight_number for l in res.legs] == ["BS341", "BS999"]


def test_the_writer_hands_back_what_it_wrote(tmp_path):
    """So a caller reporting on the export need not re-parse every row."""
    out = tmp_path / "s.xlsx"
    res = asch.write_airline_schedule(
        out, [_Row("BS341", "01/09/2026", "DAC-CGP",
                   "01/09/2026 05:00 - 07:00")])
    assert isinstance(res, asch.ScheduleResult)
    assert len(res.legs) == 1
