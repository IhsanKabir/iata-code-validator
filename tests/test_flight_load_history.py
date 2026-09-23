"""Daily flight loads out of the sideways analysis workbook.

Fixtures mirror the real sheet: flights down the rows, a six-column block
per date running sideways, blanks where a flight did not operate, and
'#DIV/0!' in the load-factor column wherever capacity was missing.
"""
from datetime import date, time

import pytest
from openpyxl import Workbook

from src import flight_load_history as flh

BLOCK = ["Capacity", "STD", "Flown", "Sold", "Unsold", "Load Factor"]


def _sheet(dates, rows, title="2026"):
    """Build a sheet shaped like the real one."""
    wb = Workbook()
    ws = wb.active
    ws.title = title
    head1 = ["", ""]
    head2 = ["Flight", "Leg/Sector"]
    for label in dates:
        head1 += [label] + [""] * 5
        head2 += BLOCK
    ws.append(head1)
    ws.append(head2)
    for flight, leg, blocks in rows:
        line = [flight, leg]
        for b in blocks:
            line += list(b) + [""] * (6 - len(b))
        ws.append(line)
    return ws


# --------------------------------------------------------------------------
# the date headers
# --------------------------------------------------------------------------
def test_a_date_header_is_read_day_first():
    got, agrees = flh.parse_header_date("03/02/2026 (Tuesday)")
    assert got == date(2026, 2, 3)
    assert agrees is True                 # the 3rd really is a Tuesday


def test_a_weekday_that_contradicts_the_date_is_reported():
    """If the label says Monday and a day-first reading gives a Tuesday, the
    sheet is month-first or the label is wrong. Either way it must not be
    quietly averaged in."""
    _got, agrees = flh.parse_header_date("03/02/2026 (Monday)")
    assert agrees is False


def test_a_header_without_a_weekday_is_accepted():
    got, agrees = flh.parse_header_date("20/09/2026")
    assert got == date(2026, 9, 20) and agrees is True


def test_rubbish_in_the_header_is_not_a_date():
    assert flh.parse_header_date("Destination: AUH") == (None, False)
    assert flh.parse_header_date(None) == (None, False)


def test_an_impossible_date_is_refused():
    assert flh.parse_header_date("31/02/2026 (Sunday)")[0] is None


# --------------------------------------------------------------------------
# a blank block is not a load of zero
# --------------------------------------------------------------------------
def test_a_flight_that_did_not_operate_is_skipped_not_counted_as_empty():
    """Averaging a zero into a route's load factor drags it down for days
    the flight never flew."""
    ws = _sheet(["03/02/2026 (Tuesday)", "04/02/2026 (Wednesday)"], [
        ("BS101", "DAC-CGP", [(72, "07:10", 70), ("", "", "")]),
    ])
    h = flh.LoadHistory()
    flh.parse_sheet(ws, h)
    assert len(h.legs) == 1
    assert h.not_operated == 1
    assert h.legs[0].flight_date == date(2026, 2, 3)


def test_the_load_factor_is_recomputed_not_read_from_the_sheet():
    """The sheet's own column holds '#DIV/0!' wherever capacity was blank."""
    ws = _sheet(["03/02/2026 (Tuesday)"], [
        ("BS101", "DAC-CGP", [(72, "07:10", 70, "", "", "#DIV/0!")]),
    ])
    h = flh.LoadHistory()
    flh.parse_sheet(ws, h)
    assert h.legs[0].load_factor == pytest.approx(70 / 72)


def test_a_leg_with_no_flown_figure_has_no_load_factor():
    ws = _sheet(["03/02/2026 (Tuesday)"], [
        ("BS101", "DAC-CGP", [(72, "07:10", "")]),
    ])
    h = flh.LoadHistory()
    flh.parse_sheet(ws, h)
    assert h.legs[0].load_factor is None
    assert h.legs[0].operated is True        # it flew; we just lack the count


# --------------------------------------------------------------------------
# the shape of a leg
# --------------------------------------------------------------------------
def test_a_leg_splits_into_origin_and_destination():
    ws = _sheet(["03/02/2026 (Tuesday)"], [
        ("BS101", "DAC-CGP", [(72, "07:10", 70)]),
    ])
    h = flh.LoadHistory()
    flh.parse_sheet(ws, h)
    leg = h.legs[0]
    assert (leg.origin, leg.destination) == ("DAC", "CGP")
    assert leg.flight_no == "BS101"


def test_a_time_cell_is_read_whether_it_is_text_or_a_time():
    ws = _sheet(["03/02/2026 (Tuesday)"], [
        ("BS101", "DAC-CGP", [(72, time(7, 10), 70)]),
        ("BS102", "CGP-DAC", [(72, "08:35", 67)]),
    ])
    h = flh.LoadHistory()
    flh.parse_sheet(ws, h)
    assert {x.std for x in h.legs} == {"07:10", "08:35"}


# --------------------------------------------------------------------------
# many dates, many flights
# --------------------------------------------------------------------------
def test_every_date_block_becomes_its_own_row():
    ws = _sheet(["03/02/2026 (Tuesday)", "04/02/2026 (Wednesday)",
                 "05/02/2026 (Thursday)"], [
        ("BS101", "DAC-CGP", [(72, "07:10", 70), (72, "07:10", 65),
                              (72, "07:10", 71)]),
        ("BS102", "CGP-DAC", [(72, "08:35", 60), (72, "08:35", 62),
                              (72, "08:35", 64)]),
    ])
    h = flh.LoadHistory()
    flh.parse_sheet(ws, h)
    assert len(h.legs) == 6
    assert h.span == (date(2026, 2, 3), date(2026, 2, 5))
    assert h.routes == {"DAC-CGP", "CGP-DAC"}


def test_a_route_can_be_pulled_out_on_its_own():
    ws = _sheet(["03/02/2026 (Tuesday)"], [
        ("BS101", "DAC-CGP", [(72, "07:10", 70)]),
        ("BS311", "DAC-CCU", [(72, "17:00", 69)]),
    ])
    h = flh.LoadHistory()
    flh.parse_sheet(ws, h)
    got = h.for_route("dac-ccu")
    assert len(got) == 1 and got[0].flight_no == "BS311"


def test_a_row_with_no_flight_number_is_ignored():
    ws = _sheet(["03/02/2026 (Tuesday)"], [
        ("", "DAC-CGP", [(72, "07:10", 70)]),
        ("BS101", "", [(72, "07:10", 70)]),
    ])
    h = flh.LoadHistory()
    flh.parse_sheet(ws, h)
    assert h.legs == []


def test_the_summary_line_says_what_was_read():
    ws = _sheet(["03/02/2026 (Tuesday)"], [
        ("BS101", "DAC-CGP", [(72, "07:10", 70)]),
    ])
    h = flh.LoadHistory()
    flh.parse_sheet(ws, h)
    said = h.summary()
    assert "1 operated flight-leg" in said
    assert "1 route" in said
