"""Tests for Flight Loads parsing + chunking.

All fixtures synthetic — no real PNL data lives in this repo.
"""

from __future__ import annotations

import pytest

from src.zenith_client import (
    iter_date_chunks,
    parse_flight_loads_html,
)


def _flight_header(flight: str = "BS346", dow: str = "Sun",
                   date: str = "24/05/2026", time: str = "01:30",
                   aircraft: str = "Boeing 737-800 - S2-AJE",
                   tickets: str = "152") -> str:
    return (
        f'<b>{flight}      - '
        f'<font color="white">{dow} {date}&nbsp;{time}'
        f'<font class="FNTListRow">&nbsp;{aircraft}</font></b>'
        f' - <b>{tickets} Tickets issued</b></font>'
    )


def _leg_info_row(route: str, time_range: str, cabin: str = "Economy") -> str:
    """One `<tr class="info">` row inside the leg table."""
    return (
        f'<tr class="info">'
        f'<td><u>{route}</u></td>'
        f'<td class="TDListRow trajet">leg</td>'
        f'<td class="TDListRow heure"><font color="blue">{time_range}</font></td>'
        f'<td class="TDListRow stock">Cabin<nobr>{cabin}</nobr></td>'
        f'</tr>'
    )


def _billets_info_row(issued: str, wl: str) -> str:
    """One `<tr class="billets">` row inside the billets table."""
    return (
        f'<tr class="billets">'
        f'<td class="TDListRow emis"><div>Issued</div>'
        f'<font class="FNTListRow">{issued}</font></td>'
        f'<td class="TDListRow emis-wl"><div>Issued WL</div>'
        f'<font class="FNTListRow">{wl}</font></td>'
        f'</tr>'
    )


def _sieges_info_row(confirmed: str, options: str, wl: str, available: str) -> str:
    """One `<TR class="sieges">` row inside the seats table."""
    return (
        f'<tr class="sieges">'
        f'<td class="TDListRow confirm"><div>Confirmed</div>'
        f'<font class="FNTListRow">{confirmed}</font></td>'
        f'<td class="TDListRow options"><div>Options</div>'
        f'<font class="FNTListRow">{options}</font></td>'
        f'<td class="TDListRow wl"><div>WL</div>'
        f'<font class="FNTListRow">{wl}</font></td>'
        f'<td class="TDListRow td-dispo reste"><div>Available</div>'
        f'<font class="FNTListRow"><font color="red">{available}</font></td>'
        f'</tr>'
    )


def _inventory_table(id_vol: str, title: str) -> str:
    return (
        f'<table data-id-vol="{id_vol}">'
        f'<tr><td class="inventorystatus" title={title}>x</td></tr></table>'
    )


def _build_flight_block(flight: str, legs: list[tuple]) -> str:
    """Build a multi-stop flight block matching real Zenith markup.

    Real Zenith wraps every stop in ONE leg-table + ONE billets-table +
    ONE seats-table, each containing N sub-rows (one per stop). Earlier
    fixture versions created separate tables per leg which never
    matched what the server actually emits.

    `legs` is a list of:
      (id_vol, route, time_range, cabin, issued, wl, confirmed, options,
       seats_wl, avail, inv_title)
    """
    # All stops on a flight share the same parent id_vol.
    id_vol = legs[0][0]
    parts = [_flight_header(flight=flight)]
    # Inventory cells (one per stop) come first in real markup.
    for leg in legs:
        parts.append(_inventory_table(id_vol, leg[10]))
    # Single leg-table containing all stops' info rows.
    parts.append(f'<table data-table-leg data-id-vol="{id_vol}">')
    for leg in legs:
        parts.append(_leg_info_row(leg[1], leg[2], leg[3]))
    parts.append("</table>")
    # Single billets-table containing all stops' billets rows.
    parts.append(f'<table data-table-billets data-id-vol="{id_vol}">')
    for leg in legs:
        parts.append(_billets_info_row(leg[4], leg[5]))
    parts.append("</table>")
    # Single seats-table containing all stops' sieges rows.
    parts.append(f'<table data-table-zs data-id-vol="{id_vol}">')
    for leg in legs:
        parts.append(_sieges_info_row(leg[6], leg[7], leg[8], leg[9]))
    parts.append("</table>")
    return "".join(parts)


def test_parse_single_flight_single_leg():
    block = _build_flight_block("BS346", legs=[(
        "111", "SHJ-DAC", "24/05/2026 01:30 - 08:05", "Economy",
        "152(0)", "0(0)", "[152]", "0(0)", "[0]", "0/152 100%",
        "AS-Flight-open",
    )])
    rows = parse_flight_loads_html(block)
    assert len(rows) == 1
    r = rows[0]
    assert r.flight_number == "BS346"
    assert r.flight_date == "24/05/2026"
    assert r.aircraft == "Boeing 737-800"
    assert r.registration == "S2-AJE"
    assert r.total_tickets_issued == "152"
    assert r.leg_route == "SHJ-DAC"
    assert r.leg_origin == "SHJ"
    assert r.leg_destination == "DAC"
    assert r.leg_cabin == "Economy"
    assert r.leg_local_time_range == "24/05/2026 01:30 - 08:05"
    assert r.tickets_issued == "152(0)"
    assert r.tickets_wl == "0(0)"
    assert r.seats_confirmed == "[152]"
    assert r.seats_options == "0(0)"
    assert r.seats_wl == "[0]"
    assert r.seats_available == "0/152 100%"
    assert r.inventory_status == "AS-Flight-open"


def test_parse_multi_leg_flight():
    block = _build_flight_block("BS346", legs=[
        ("111", "DAC-MCT", "24/05/2026 20:05 - 25/05/2026 01:00", "Economy",
         "15(0)", "0(0)", "[27]", "12(0)", "[0]", "159/186 14%", "AS-open"),
        ("222", "MCT-DAC", "24/05/2026 02:00 - 10:20", "Economy",
         "25(0)", "0(0)", "[25]", "0(0)", "[0]", "144/169 14%", "AS-open"),
    ])
    rows = parse_flight_loads_html(block)
    assert len(rows) == 2
    assert rows[0].leg_route == "DAC-MCT"
    assert rows[1].leg_route == "MCT-DAC"
    assert rows[0].tickets_issued == "15(0)"
    assert rows[1].tickets_issued == "25(0)"


def test_parse_multiple_flights_in_one_response():
    html = (
        _build_flight_block("BS346", legs=[(
            "111", "DAC-DXB", "24/05/2026 00:25 - 07:10", "Economy",
            "397(0)", "0(0)", "[397]", "0(0)", "[0]", "13/410 97%", "AS-open",
        )])
        + _build_flight_block("BS342", legs=[(
            "222", "DXB-DAC", "23/05/2026 00:25 - 07:10", "Economy",
            "397(0)", "0(0)", "[397]", "0(0)", "[0]", "0/410 97%", "AS-open",
        )])
    )
    rows = parse_flight_loads_html(html)
    assert len(rows) == 2
    assert rows[0].flight_number == "BS346"
    assert rows[1].flight_number == "BS342"


def test_aircraft_split_handles_atr_dash_prefix():
    """ATR 72 - 600 - S2-AKJ should split as aircraft='ATR 72 - 600', reg='S2-AKJ'."""
    block = _build_flight_block("BS101", legs=[(
        "111", "DAC-CXB", "24/05/2026 07:00 - 08:05", "Economy",
        "71(0)", "0(0)", "[71]", "0(0)", "[0]", "1/72 99%", "AS-open",
    )]).replace(
        "Boeing 737-800 - S2-AJE", "ATR 72 - 600 - S2-AKJ",
    )
    rows = parse_flight_loads_html(block)
    assert rows[0].aircraft == "ATR 72 - 600"
    assert rows[0].registration == "S2-AKJ"


def test_parse_empty_page():
    rows = parse_flight_loads_html("<html><body>no flights</body></html>")
    assert rows == []


def test_multi_stop_flight_emits_one_row_per_stop():
    """Regression: BS342 on 24/05 has 3 stops (DXB-CGP, CGP-DAC, DXB-DAC)
    sharing one id_vol — earlier code only emitted the first stop because
    it walked `data-table-leg` blocks instead of `<tr class="info">` rows
    inside the single leg table.
    """
    block = _build_flight_block("BS342", legs=[
        ("999", "DXB-CGP", "26/05/2026 02:55 - 09:40", "Economy",
         "117(35)", "0(0)", "[152]", "0(0)", "[0]", "0/152 100%", "AS-open"),
        ("999", "CGP-DAC", "26/05/2026 10:30 - 11:25", "Economy",
         "38(35)", "0(0)", "[73]", "0(0)", "[0]", "79/152 48%", "AS-open"),
        ("999", "DXB-DAC", "26/05/2026 02:55 - 11:25", "Economy",
         "35(0)", "0(0)", "[35]", "0(0)", "[0]", "0/35 100%", "AS-open"),
    ])
    rows = parse_flight_loads_html(block)
    routes = [r.leg_route for r in rows]
    assert routes == ["DXB-CGP", "CGP-DAC", "DXB-DAC"], (
        f"multi-stop should yield 3 rows, got: {routes}"
    )
    # Per-row metrics must come from the matching sub-rows, not the first.
    assert rows[1].tickets_issued == "38(35)"
    assert rows[1].seats_available == "79/152 48%"
    assert rows[2].tickets_issued == "35(0)"
    # Inventory cells must pair by position too.
    assert all(r.inventory_status == "AS-open" for r in rows)


def test_flight_header_whitespace_tolerant():
    """Legacy markup sometimes pads the flight number: `<b>BS        308`.

    This used to absorb the trailing flight's legs into the previous
    flight because the header regex required no whitespace between
    `BS` and the digits. The fix tolerates whitespace.
    """
    first = _build_flight_block("BS346", legs=[(
        "111", "SHJ-DAC", "24/05/2026 01:30 - 08:05", "Economy",
        "152(0)", "0(0)", "[152]", "0(0)", "[0]", "0/152 100%", "AS-open",
    )])
    second = _build_flight_block("BS308", legs=[(
        "222", "SIN-DAC", "24/05/2026 05:40 - 07:50", "Economy",
        "158(0)", "0(0)", "[158]", "0(0)", "[0]", "-6/152 104%", "AS-open",
    )])
    # Mangle the BS308 header the way Zenith sometimes does
    second_mangled = second.replace("<b>BS308", "<b>BS        308")
    html = first + second_mangled
    rows = parse_flight_loads_html(html)
    flight_nums = [r.flight_number for r in rows]
    assert "BS346" in flight_nums
    assert "BS308" in flight_nums, "BS308 must be captured even with padded header"
    # And BS346 must NOT have eaten BS308's legs
    bs346_routes = [r.leg_route for r in rows if r.flight_number == "BS346"]
    assert bs346_routes == ["SHJ-DAC"]


# ---- chunking ----


def test_iter_date_chunks_evenly_divides():
    chunks = list(iter_date_chunks("01/05/2026", "10/05/2026", chunk_days=5))
    assert chunks == [("01/05/2026", "05/05/2026"), ("06/05/2026", "10/05/2026")]


def test_iter_date_chunks_partial_last():
    chunks = list(iter_date_chunks("01/05/2026", "07/05/2026", chunk_days=5))
    assert chunks == [("01/05/2026", "05/05/2026"), ("06/05/2026", "07/05/2026")]


def test_iter_date_chunks_single_day_chunks():
    chunks = list(iter_date_chunks("01/05/2026", "03/05/2026", chunk_days=1))
    assert chunks == [
        ("01/05/2026", "01/05/2026"),
        ("02/05/2026", "02/05/2026"),
        ("03/05/2026", "03/05/2026"),
    ]


def test_iter_date_chunks_inverted_range_empty():
    chunks = list(iter_date_chunks("10/05/2026", "01/05/2026", chunk_days=5))
    assert chunks == []


def test_iter_date_chunks_same_day():
    chunks = list(iter_date_chunks("01/05/2026", "01/05/2026", chunk_days=10))
    assert chunks == [("01/05/2026", "01/05/2026")]
