"""Rebuilding the fleet from a timetable that names types, not tails."""
from datetime import date

import pytest
from openpyxl import load_workbook

from src import fleet_rotation as fr
from src import route_optimisation as ro
from src import route_optimisation_report as ror

MON = date(2026, 9, 7)
BLOCKS = {"DAC-CGP": 55, "CGP-DAC": 55, "DAC-CCU": 60, "CCU-DAC": 60,
          "DAC-DOH": 350, "DOH-DAC": 300}


class _Leg:
    def __init__(self, route, std, when=MON, ac="ATR-72-600"):
        self.leg_route, self.std, self.flight_date = route, std, when
        self.aircraft = ac


def _day(when=MON):
    """One ATR on DAC-CGP, a second on two Kolkata trips. Kolkata times
    are India local, half an hour behind: the second ATR lands back at
    Dhaka 09:45 and sits there, ready from 10:10, until 17:00."""
    return [_Leg("DAC-CGP", "07:00", when), _Leg("CGP-DAC", "08:25", when),
            _Leg("DAC-CGP", "10:00", when), _Leg("CGP-DAC", "11:30", when),
            _Leg("DAC-CCU", "07:00", when), _Leg("CCU-DAC", "08:15", when),
            _Leg("DAC-CCU", "17:00", when), _Leg("CCU-DAC", "18:15", when)]


def test_spellings_of_one_type_are_one_fleet():
    assert fr.family("ATR-72-600") == fr.family("ATR-72") == "ATR 72"
    assert fr.family("Boeing-737-800") == fr.family("Boeing 737-800")


def test_the_737_and_a320_are_one_pool_because_return_legs_are_mislabelled():
    """SHJ-DAC reads '737-800' on nights the A320 flew out."""
    assert fr.family("Airbus A320") == fr.family("Boeing 737-800")


def test_block_times_are_corrected_for_the_clocks():
    """The search says DAC-CCU is 30 minutes and CCU-DAC 90: local times."""
    rows = [{"airline": "BS", "origin": o, "destination": d,
             "queried_origin": o, "_minutes": m}
            for o, d, m in (("DAC", "CCU", 30), ("CCU", "DAC", 90),
                            ("DAC", "CCU", 300))]
    got = fr.block_times(rows)
    assert got == {"DAC-CCU": 60, "CCU-DAC": 60}      # shortest kept


def test_block_times_ignore_other_airlines():
    rows = [{"airline": "6E", "origin": "DAC", "destination": "CCU",
             "queried_origin": "DAC", "_minutes": 30}]
    assert fr.block_times(rows) == {}


def test_legs_that_follow_each_other_share_an_aircraft():
    fleet = fr.chain(_day(), BLOCKS)
    assert fleet.proven("ATR 72") == 2
    assert fr.chain_breaks(fleet, "ATR 72") == 0


def test_a_foreign_departure_time_is_read_as_local():
    """DOH-DAC leaves 23:30 on the day DAC-DOH left 20:10. In Dhaka time
    that is before the aircraft has even landed; in Doha time it follows."""
    legs = [_Leg("DAC-DOH", "20:10", ac="Boeing 737-800"),
            _Leg("DOH-DAC", "23:30", ac="Boeing 737-800")]
    fleet = fr.chain(legs, {"DAC-DOH": 330, "DOH-DAC": 300})
    assert fleet.proven(fr.NARROWBODY) == 1


def test_the_busiest_day_proves_the_fleet():
    legs = _day(MON) + _day(date(2026, 9, 8))[:4]     # Tuesday: one ATR
    fleet = fr.chain(legs, BLOCKS)
    assert fleet.proven("ATR 72") == 2
    assert fleet.flying_on("ATR 72", date(2026, 9, 8)) == 1


def test_a_ground_gap_at_the_base_is_a_slot():
    fleet = fr.chain(_day(), BLOCKS)
    slots = fr.slots_for(fleet, "ATR 72", "DAC", rotation=170)
    assert slots[0].gap is not None and slots[0].fits
    # The first ATR, back from Chittagong 12:25 plus 25 minutes' ground,
    # waits until the 17:00 Kolkata. The second's wait from 10:10 has no
    # leg after it in the data, so its end is unknown and it is not
    # counted: a slot is claimed only between two recorded legs.
    assert slots[0].gap == 12 * 60 + 50


def test_a_rotation_longer_than_any_gap_does_not_fit():
    fleet = fr.chain(_day(), BLOCKS)
    slots = fr.slots_for(fleet, "ATR 72", "DAC", rotation=12 * 60)
    assert slots[0].gap is None and not slots[0].fits


def test_an_aircraft_idle_all_day_is_shown_as_spare_not_as_a_gap():
    legs = _day(MON) + _day(date(2026, 9, 8))[:4]
    fleet = fr.chain(legs, BLOCKS)
    tue = fr.slots_for(fleet, "ATR 72", "DAC", rotation=12 * 60)[1]
    assert tue.spare is True and tue.gap is None


def test_one_more_rotation_is_checked_weekday_by_weekday():
    fleet = fr.chain(_day(), BLOCKS)
    got = fr.check_rotation(fleet, _day(), BLOCKS, "DAC-CCU")
    assert got.fam == "ATR 72"
    assert got.rotation == 60 + 25 + 60 + 25
    assert got.flown_days == ("Mon",)
    gaps, spares, seen, start, planes = got.by_weekday["Mon"]
    assert (gaps, seen, planes) == (1, 1, 1) and start is not None


def test_an_unknown_route_cannot_be_checked():
    fleet = fr.chain(_day(), BLOCKS)
    assert fr.check_rotation(fleet, _day(), BLOCKS, "DAC-XYZ") is None


def test_the_workbook_gets_a_fleet_sheet(tmp_path):
    fleet = fr.chain(_day(), BLOCKS)
    check = fr.check_rotation(fleet, _day(), BLOCKS, "DAC-CCU")
    out = tmp_path / "ro.xlsx"
    ror.build_workbook(ro.Result(), out, fleet, [check])
    ws = load_workbook(out)["Fleet"]
    text = " ".join(str(c.value) for row in ws.iter_rows() for c in row
                    if c.value is not None)
    assert "DAC ⇄ CCU" in text and "ATR 72" in text and "(1/1)" in text


def test_no_fleet_means_no_fleet_sheet(tmp_path):
    out = tmp_path / "ro.xlsx"
    ror.build_workbook(ro.Result(), out)
    assert "Fleet" not in load_workbook(out).sheetnames


@pytest.mark.parametrize("text,want", [("07:10", 430), ("7:05", 425),
                                       ("", None), (None, None)])
def test_clock_reading(text, want):
    assert fr._clock(text) == want


def test_an_atr_free_only_late_at_night_is_not_a_slot():
    """Parked for the night after a 22:10 landing is not a Kolkata slot."""
    legs = [_Leg("DAC-CGP", "20:30"), _Leg("CGP-DAC", "21:50"),
            _Leg("DAC-CGP", "07:00", date(2026, 9, 8))]
    fleet = fr.chain(legs, BLOCKS)
    assert fr.slots_for(fleet, "ATR 72", "DAC", rotation=170)[0].gap is None
