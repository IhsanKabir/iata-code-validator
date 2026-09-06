"""Flight-centric view of schedule changes: how often a flight moved."""
from datetime import datetime

from src import flight_schedule_history as fsh
from src.flight_change_auth import KIND_CANCEL, KIND_TIME, KIND_TRANSFER


class _Flight:
    def __init__(self, number="", origin="", destination="", date=""):
        self.flight_number, self.origin = number, origin
        self.destination, self.flight_date = destination, date
        self.departure_time = ""


class _Agent:
    def __init__(self, name="", dept=""):
        self.display_name, self.department = name, dept
        self.user_id = ""


class _Event:
    """Stands in for a parsed history row."""

    def __init__(self, desc, *, when=None, pnr="", flight=None,
                 etype="Changing flight time", agent=None):
        self.raw_description, self.event_type = desc, etype
        self.timestamp = when
        self.pnr, self.passenger, self.customer = pnr, "", ""
        self.flight = flight or _Flight()
        self.raw_flight = ""
        self.agent = agent or _Agent()


def _time_change(route="DACRUH", date="28/07/2026", old="12:55", new="13:55",
                 arr="17:10"):
    return (f"{route}/{date}/ {old}->{arr} ==> {date}/ {new}->{arr}")


TRANSFER = ("Flight transfer:<br>Reason: DELAY<br>"
            "Flight:BS 381 28/07/2026->BS 381 30/08/2026")


# --- W1: a transfer's "shift" is a date gap and must never be averaged -----

def test_a_transfers_date_gap_never_enters_a_magnitude_average():
    """Measured on the real export: a time revision carries 65 minutes and a
    transfer carries 47,520 -- the gap between two dates. Averaged together
    that reads as a three-thousand-minute delay, which is a fact about
    nothing."""
    flight = _Flight("BS381", "DAC", "RUH", "28/07/2026")
    events = [
        _Event(_time_change(), when=datetime(2026, 7, 1, 9, 0), pnr="0A1",
               flight=flight),
        _Event(TRANSFER, when=datetime(2026, 7, 2, 9, 0), pnr="0A1",
               flight=flight, etype="Flight transfer"),
    ]
    res = fsh.aggregate(events)
    route = res.routes["DAC-RUH"]

    assert res.n_time == 1 and res.n_swaps == 1
    # the only magnitude used is the 60-minute move
    assert route._shifts == [60.0]
    assert route.mean_shift == 60.0
    assert route.worst_shift == 60.0


def test_a_transfer_is_counted_but_carries_no_direction():
    flight = _Flight("BS381", "DAC", "RUH", "28/07/2026")
    res = fsh.aggregate([_Event(TRANSFER, when=datetime(2026, 7, 2, 9, 0),
                                flight=flight, etype="Flight transfer")])
    (change,) = res.changes
    assert change.kind == KIND_TRANSFER
    assert change.direction == ""          # no "later"/"earlier" for a swap
    assert not change.is_measurable


# --- the counts the whole feature exists to produce -----------------------

def test_it_counts_how_many_times_one_flight_moved():
    flight = _Flight("BS381", "DAC", "RUH", "28/07/2026")
    events = [
        _Event(_time_change(new="13:55"), when=datetime(2026, 7, 1, 9, 0),
               pnr="0A1", flight=flight),
        _Event(_time_change(new="14:55"), when=datetime(2026, 7, 2, 9, 0),
               pnr="0A1", flight=flight),
        _Event(TRANSFER, when=datetime(2026, 7, 3, 9, 0), pnr="0A1",
               flight=flight, etype="Flight transfer"),
    ]
    res = fsh.aggregate(events)
    rec = res.flights[("BS381", "28/07/2026")]
    assert rec.n_changes == 3
    assert rec.n_time == 2
    assert rec.n_swaps == 1


def test_direction_and_magnitude_come_from_the_signed_shift():
    flight = _Flight("BS381", "DAC", "RUH", "28/07/2026")
    later = _Event(_time_change(old="12:55", new="13:55"),
                   when=datetime(2026, 7, 1, 9, 0), flight=flight)
    earlier = _Event(_time_change(old="12:55", new="11:55"),
                     when=datetime(2026, 7, 2, 9, 0), flight=flight)
    res = fsh.aggregate([later, earlier])
    dirs = {c.direction for c in res.changes}
    assert dirs == {"later", "earlier"}
    assert res.routes["DAC-RUH"].moved_later == 1
    assert res.routes["DAC-RUH"].moved_earlier == 1


# --- W3: never present a de-duplication that was not observed -------------

def test_one_change_written_against_many_pnrs_collapses_to_one():
    """A per-flight export repeats the same change once per affected PNR. The
    size of the group is the passengers affected."""
    flight = _Flight("BS381", "DAC", "RUH", "28/07/2026")
    when = datetime(2026, 7, 1, 9, 0)
    events = [_Event(_time_change(), when=when, pnr=p, flight=flight)
              for p in ("0A1", "0A2", "0A3")]
    res = fsh.aggregate(events)

    assert len(res.changes) == 1               # one change, not three
    (change,) = res.changes
    assert change.rows == 3
    assert change.passengers == 3
    assert res.repetition_observed


def test_without_repetition_the_passenger_count_is_flagged_as_a_floor():
    flight = _Flight("BS381", "DAC", "RUH", "28/07/2026")
    res = fsh.aggregate([_Event(_time_change(),
                                when=datetime(2026, 7, 1, 9, 0),
                                pnr="0A1", flight=flight)])
    assert res.changes[0].rows == 1
    assert not res.repetition_observed         # the report must say so


def test_two_changes_a_minute_apart_stay_two_changes():
    """Timestamps in these exports are minute-granular, so the minute is the
    native resolution -- distinct minutes are distinct changes."""
    flight = _Flight("BS381", "DAC", "RUH", "28/07/2026")
    res = fsh.aggregate([
        _Event(_time_change(new="13:55"), when=datetime(2026, 7, 1, 9, 0),
               flight=flight),
        _Event(_time_change(new="13:55"), when=datetime(2026, 7, 1, 9, 1),
               flight=flight),
    ])
    assert len(res.changes) == 2


# --- W4: a transfer carries no route of its own ---------------------------

def test_a_swap_is_filed_under_the_flights_route_not_unknown():
    """Verified against real data: FLIGHT_TRANSFER comes back with route=''.
    Reading the change alone would file every swap under 'unknown'."""
    flight = _Flight("BS381", "DAC", "RUH", "28/07/2026")
    res = fsh.aggregate([_Event(TRANSFER, when=datetime(2026, 7, 2, 9, 0),
                                flight=flight, etype="Flight transfer")])
    assert list(res.routes) == ["DAC-RUH"]


# --- W5: never invent a denominator ---------------------------------------

def test_share_changed_is_none_until_a_roster_supplies_the_denominator():
    flight = _Flight("BS381", "DAC", "RUH", "28/07/2026")
    res = fsh.aggregate([_Event(_time_change(),
                                when=datetime(2026, 7, 1, 9, 0),
                                flight=flight)])
    assert res.roster_loaded is False
    assert res.routes["DAC-RUH"].share_changed is None


def test_a_roster_turns_the_share_into_a_real_fraction():
    class _Ref:
        def __init__(s, o, d):
            s.origin, s.destination = o, d

    flight = _Flight("BS381", "DAC", "RUH", "28/07/2026")
    roster = [_Ref("DAC", "RUH") for _ in range(4)]
    res = fsh.aggregate([_Event(_time_change(),
                                when=datetime(2026, 7, 1, 9, 0),
                                flight=flight)], roster=roster)
    assert res.roster_loaded is True
    assert res.routes["DAC-RUH"].share_changed == 0.25   # 1 of 4 moved


# --- W7: a blank flight cell must not become a second flight --------------

def test_a_blank_flight_cell_is_recovered_from_the_same_route_and_date():
    """Two of five real rows named the change and left the flight cell empty.
    Filing those under 'not written' split one flight across two buckets and
    counted it as two flights."""
    named = _Flight("BS381", "DAC", "RUH", "28/07/2026")
    events = [
        _Event(_time_change(), when=datetime(2026, 7, 1, 9, 0), pnr="0A1",
               flight=named),
        _Event(_time_change(new="14:55"), when=datetime(2026, 7, 2, 9, 0),
               pnr="0A1", flight=_Flight()),          # blank cell
    ]
    res = fsh.aggregate(events)
    assert {c.flight_number for c in res.changes} == {"BS381"}
    assert len(res.flights) == 1
    assert res.routes["DAC-RUH"].flights_changed == 1


def test_a_flight_number_is_never_invented_for_a_route_it_was_not_seen_on():
    """Recovery adopts a number actually observed on that route and date.
    With nothing to adopt, the route identifies the flight -- no fiction."""
    res = fsh.aggregate([_Event(_time_change(),
                                when=datetime(2026, 7, 1, 9, 0),
                                pnr="0A1", flight=_Flight())])
    (change,) = res.changes
    assert change.flight_number == "DAC-RUH"      # the route, not a made-up BS
    assert not change.flight_number.startswith("BS")


# --- lead time -------------------------------------------------------------

def test_lead_time_measures_notice_given_before_the_departure_it_moved():
    flight = _Flight("BS381", "DAC", "RUH", "28/07/2026")
    # change made on 27 July for a 28 July 12:55 departure -> under 24 hours
    res = fsh.aggregate([_Event(_time_change(),
                                when=datetime(2026, 7, 27, 18, 0),
                                flight=flight)])
    (change,) = res.changes
    assert change.lead == "under 24 hours"
    assert 0 < change.lead_minutes < 24 * 60


def test_a_change_made_after_departure_is_labelled_not_hidden():
    flight = _Flight("BS381", "DAC", "RUH", "28/07/2026")
    res = fsh.aggregate([_Event(_time_change(),
                                when=datetime(2026, 8, 1, 9, 0),
                                flight=flight)])
    assert res.changes[0].lead == "after departure"


def test_lead_is_not_known_when_the_change_carries_no_timestamp():
    flight = _Flight("BS381", "DAC", "RUH", "28/07/2026")
    res = fsh.aggregate([_Event(_time_change(), when=None, flight=flight)])
    assert res.changes[0].lead == "not known"


# --- the blind spot stays visible -----------------------------------------

def test_events_carrying_no_recognised_change_are_counted_not_dropped():
    res = fsh.aggregate([
        _Event("Seat 12A assigned", when=datetime(2026, 7, 1, 9, 0),
               etype="Seat map change"),
        _Event("Fare recalculated", when=datetime(2026, 7, 1, 9, 0),
               etype="Ticket Modification"),
    ])
    assert res.changes == []
    assert res.unrecognised == {"Seat map change": 1, "Ticket Modification": 1}
    assert res.events_read == 2 and res.events_with_change == 0


def test_every_event_lands_somewhere():
    """Counted, or recorded as unrecognised -- never silently dropped."""
    flight = _Flight("BS381", "DAC", "RUH", "28/07/2026")
    events = [
        _Event(_time_change(), when=datetime(2026, 7, 1, 9, 0), flight=flight),
        _Event("nothing here", when=datetime(2026, 7, 1, 9, 0),
               etype="Ticket Modification"),
    ]
    res = fsh.aggregate(events)
    assert res.events_read == 2
    assert res.events_with_change + sum(res.unrecognised.values()) == 2


def test_the_aggregation_is_deterministic():
    flight = _Flight("BS381", "DAC", "RUH", "28/07/2026")
    events = [
        _Event(_time_change(new=t), when=datetime(2026, 7, d, 9, 0),
               pnr=p, flight=flight)
        for d, t, p in ((1, "13:55", "0A1"), (2, "14:55", "0A2"),
                        (3, "15:55", "0A3"))
    ]
    a = fsh.aggregate(events)
    b = fsh.aggregate(events)
    shape = lambda r: [(c.when, c.flight_number, c.kind, c.shift_minutes)
                       for c in r.changes]
    assert shape(a) == shape(b)


def test_nothing_at_all_is_not_an_error():
    res = fsh.aggregate([])
    assert res.changes == [] and res.routes == {} and res.flights == {}
    assert res.passengers == 0 and not res.repetition_observed
