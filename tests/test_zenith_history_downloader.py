"""Tests for the Zenith history downloader.

We exercise the parser + file-naming logic with inline synthetic HTML
that mirrors the gestionregulation listing format. The network bits
(list_flights, download_history_file) are integration-only and live
behind a manual smoke test, not the unit-test suite.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from src.zenith_history_downloader import (
    FlightRef,
    parse_flight_list,
)


def _flight_row(
    id_vol: int, airline: str, number: int,
    date: str, orig: str, dest: str, time: str = "05:00-->07:10",
    aircraft: str = "Boeing 737-800",
) -> str:
    """Render a fake gestionregulation listing row with a visu_PlanVolLeg call."""
    return textwrap.dedent(f"""
    <tr>
      <td>
        <a onclick="javascript:visu_PlanVolLeg({id_vol},73,'[{airline}     {number}].[24 {date}].[{orig}-{dest}].[{time}].[{aircraft}]')">link</a>
        <button onclick='openSecondaryFrame("/newui/aerien/commun/search_event.asp?contexte=Liste_vols&id_vol={id_vol}&CategorieEvent=1")'>History</button>
      </td>
    </tr>
    """)


def test_parse_flight_list_extracts_basics():
    html = _flight_row(123456, "BS", 326, "24/05/2026", "CAN", "DAC")
    flights = parse_flight_list(html)
    assert len(flights) == 1
    f = flights[0]
    assert f.id_vol == "123456"
    assert f.flight_number == "BS326"
    assert f.flight_date == "24/05/2026"
    assert f.origin == "CAN"
    assert f.destination == "DAC"


def test_parse_flight_list_dedupes_repeated_id_vol():
    """Multi-leg flights re-emit the same id_vol — keep one row only."""
    row = _flight_row(999, "BS", 341, "01/01/2026", "DAC", "DXB")
    html = row + row + row
    flights = parse_flight_list(html)
    assert [f.id_vol for f in flights] == ["999"]


def test_parse_flight_list_handles_multiple_flights():
    html = (
        _flight_row(1001, "BS", 326, "01/05/2026", "CAN", "DAC")
        + _flight_row(1002, "BS", 322, "01/05/2026", "MCT", "DAC")
        + _flight_row(1003, "BS", 308, "01/05/2026", "SIN", "DAC")
    )
    flights = parse_flight_list(html)
    assert [f.id_vol for f in flights] == ["1001", "1002", "1003"]
    assert [f.flight_number for f in flights] == ["BS326", "BS322", "BS308"]


def test_parse_flight_list_returns_empty_for_blank_input():
    assert parse_flight_list("<html></html>") == []


@pytest.mark.parametrize(
    ("date", "expected"),
    [
        ("01/01/2026", "ModificationHistory 1 JAN DAC-DXB.xls"),
        ("05/02/2026", "ModificationHistory 5 FEB DAC-DXB.xls"),
        ("18/03/2026", "ModificationHistory 18 MAR DAC-DXB.xls"),
        ("24/12/2026", "ModificationHistory 24 DEC DAC-DXB.xls"),
    ],
)
def test_filename_matches_manual_export_convention(date, expected):
    """The filename has to match Zenith's manual-export naming exactly so
    the existing analyzer picks up new downloads without reconfig.
    """
    f = FlightRef(
        id_vol="1", flight_number="BS999",
        flight_date=date, origin="DAC", destination="DXB",
    )
    assert f.filename == expected


def test_filename_strips_leading_zero_on_day():
    """Zenith's manual export omits the leading zero on day numbers."""
    f = FlightRef(
        id_vol="1", flight_number="BS1", flight_date="03/01/2026",
        origin="A", destination="B",
    )
    # Day 3 — no leading zero, single-letter codes pass through unchanged
    assert "3 JAN" in f.filename
    assert "03 JAN" not in f.filename
