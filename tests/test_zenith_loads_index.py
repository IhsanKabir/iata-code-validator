"""Tests for the Flight Loads index + load-verdict enrichment.

All fixtures are inline / tmp_path so no real flight data lives in
the repo.
"""

from __future__ import annotations

import pytest
from openpyxl import Workbook

from src.zenith_history_analyzer import (
    audit_downgrade_justification,
    run_history_audit,
)
from src.zenith_history_parser import (
    Agent,
    FlightRef,
    HistoryEvent,
)
from src.zenith_loads_index import (
    HIGH_LOAD_THRESHOLD,
    LOW_LOAD_THRESHOLD,
    LoadEntry,
    LoadLookup,
    VERDICT_JUSTIFIED,
    VERDICT_QUESTIONABLE,
    VERDICT_SITUATIONAL,
    VERDICT_UNKNOWN,
    load_verdict,
    parse_seats_available,
    read_flight_loads_excel,
)


# ---------------------------------------------------------------------------
# Cell parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("13/410 97%", (13, 410, 97.0)),
        ("0/152 100%", (0, 152, 100.0)),
        ("-5/152 103%", (-5, 152, 103.0)),     # over-booked
        ("113/436 74.5%", (113, 436, 74.5)),
        ("", None),
        ("not a load", None),
    ],
)
def test_parse_seats_available(text, expected):
    assert parse_seats_available(text) == expected


# ---------------------------------------------------------------------------
# Verdict thresholds
# ---------------------------------------------------------------------------


def test_verdict_buckets():
    assert load_verdict(95.0) == VERDICT_QUESTIONABLE
    assert load_verdict(HIGH_LOAD_THRESHOLD) == VERDICT_QUESTIONABLE
    assert load_verdict(85.0) == VERDICT_SITUATIONAL
    assert load_verdict(LOW_LOAD_THRESHOLD) == VERDICT_SITUATIONAL
    assert load_verdict(65.0) == VERDICT_JUSTIFIED
    assert load_verdict(0.0) == VERDICT_JUSTIFIED
    assert load_verdict(None) == VERDICT_UNKNOWN


def test_verdict_buckets_with_custom_thresholds():
    """Caller-supplied thresholds (e.g. from the GUI sliders) override defaults."""
    # Tighter standards: questionable above 80%, justified below 50%
    assert load_verdict(85.0, high_threshold=80.0, low_threshold=50.0) == VERDICT_QUESTIONABLE
    assert load_verdict(65.0, high_threshold=80.0, low_threshold=50.0) == VERDICT_SITUATIONAL
    assert load_verdict(40.0, high_threshold=80.0, low_threshold=50.0) == VERDICT_JUSTIFIED
    # Looser standards: questionable only above 99%, justified below 30%
    assert load_verdict(95.0, high_threshold=99.0, low_threshold=30.0) == VERDICT_SITUATIONAL


def test_audit_uses_caller_thresholds():
    """audit_downgrade_justification respects per-call thresholds."""
    from datetime import datetime
    from src.zenith_history_analyzer import audit_downgrade_justification
    from src.zenith_history_parser import Agent, FlightRef, HistoryEvent

    def _e(pnr, ts, rbd):
        return HistoryEvent(
            source_file="t.xls", row_index=0,
            raw_date=ts, raw_created_by="A (a)",
            raw_description="", event_type="Ticket Modification",
            pnr=pnr, customer="",
            raw_flight="BS341 DAC DXB 01/01/2026 00:00", passenger="X",
            timestamp=datetime.strptime(ts, "%d/%m/%Y %H:%M"),
            agent=Agent(raw="A (a)", display_name="A", user_id="a", department=""),
            flight=FlightRef(raw="", flight_number="BS341",
                            origin="DAC", destination="DXB",
                            flight_date="01/01/2026", departure_time="00:00"),
            rbd_class=rbd,
        )

    events = [_e("P1", "01/01/2026 10:00", "Y"), _e("P1", "01/01/2026 14:00", "G")]
    lookup = LoadLookup.from_entries([_entry("BS341", "01/01/2026", "DAC", "DXB", 85)])

    # Default thresholds (90/70): 85% → SITUATIONAL
    default = audit_downgrade_justification(events, lookup)
    assert default[0].verdict == VERDICT_SITUATIONAL

    # Stricter high threshold (80): same 85% → QUESTIONABLE
    strict = audit_downgrade_justification(
        events, lookup, high_threshold=80.0, low_threshold=50.0,
    )
    assert strict[0].verdict == VERDICT_QUESTIONABLE


# ---------------------------------------------------------------------------
# Excel reader
# ---------------------------------------------------------------------------


def _write_loads_xlsx(tmp_path, rows):
    """Write a minimal Flight Loads-shaped workbook for parsing tests."""
    p = tmp_path / "loads.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Flight Loads"
    ws.append([
        "Flight Number", "Day", "Flight Date", "Departure Time",
        "Aircraft", "Registration", "Total Tickets Issued",
        "Leg Route", "Origin", "Destination",
        "Leg Local Time", "Cabin",
        "Tickets Issued", "Tickets WL",
        "Seats Confirmed", "Seats Options", "Seats WL",
        "Seats Available", "Inventory Status", "Comments",
    ])
    for row in rows:
        ws.append(row)
    wb.save(p)
    return p


def test_read_flight_loads_excel_happy_path(tmp_path):
    p = _write_loads_xlsx(tmp_path, [
        ["BS341", "Thu", "01/01/2026", "20:45", "Boeing 737-800", "S2-AJE",
         "152", "DAC-DXB", "DAC", "DXB", "01/01/2026 20:45 - 02:45",
         "Economy", "100(0)", "0(0)", "[100]", "0(0)", "[0]",
         "52/152 65%", "AS-Flight open", ""],
        ["BS342", "Thu", "01/01/2026", "00:25", "Airbus A330-300", "S2-ALA",
         "397", "DXB-DAC", "DXB", "DAC", "01/01/2026 00:25 - 07:10",
         "Economy", "397(0)", "0(0)", "[397]", "0(0)", "[0]",
         "13/410 97%", "AS-Flight open", ""],
    ])
    entries = read_flight_loads_excel(p)
    assert len(entries) == 2
    bs341 = next(e for e in entries if e.flight_number == "BS341")
    assert bs341.load_pct == 65.0
    assert bs341.seats_capacity == 152
    assert bs341.cabin == "Economy"


def test_read_flight_loads_excel_skips_unparseable_load(tmp_path):
    p = _write_loads_xlsx(tmp_path, [
        ["BS999", "Mon", "01/01/2026", "00:00", "X", "Y", "0",
         "AAA-BBB", "AAA", "BBB", "01/01/2026 00:00 - 01:00",
         "Economy", "0(0)", "0(0)", "[0]", "0(0)", "[0]",
         "this isn't a load string", "", ""],
    ])
    assert read_flight_loads_excel(p) == []


def test_read_flight_loads_excel_missing_sheet(tmp_path):
    p = tmp_path / "wrong.xlsx"
    wb = Workbook()
    wb.active.title = "Some Other Sheet"
    wb.save(p)
    with pytest.raises(ValueError, match="Flight Loads"):
        read_flight_loads_excel(p)


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def _entry(
    flight, date, orig, dest, pct, cap=152, cabin="Economy",
) -> LoadEntry:
    return LoadEntry(
        flight_number=flight, flight_date=date,
        origin=orig, destination=dest, cabin=cabin,
        seats_available=int(cap * (100 - pct) / 100),
        seats_capacity=cap,
        load_pct=pct,
        inventory_status="AS-Flight open",
        raw_seats_available=f"{int(cap*(100-pct)/100)}/{cap} {pct}%",
    )


def test_lookup_exact_leg_match():
    lookup = LoadLookup.from_entries([
        _entry("BS341", "01/01/2026", "DAC", "DXB", 65),
    ])
    e = lookup.find("BS341", "01/01/2026", "DAC", "DXB")
    assert e is not None and e.load_pct == 65


def test_lookup_reverse_leg_match():
    """Some history rows have the leg reversed — accept the match."""
    lookup = LoadLookup.from_entries([
        _entry("BS341", "01/01/2026", "DAC", "DXB", 65),
    ])
    e = lookup.find("BS341", "01/01/2026", "DXB", "DAC")
    assert e is not None and e.load_pct == 65


def test_lookup_falls_back_when_leg_unknown():
    """No leg info → fall back to any leg of that flight on that date."""
    lookup = LoadLookup.from_entries([
        _entry("BS341", "01/01/2026", "DAC", "DXB", 65),
    ])
    e = lookup.find("BS341", "01/01/2026")
    assert e is not None and e.load_pct == 65


def test_lookup_returns_none_for_unknown_flight():
    lookup = LoadLookup.from_entries([
        _entry("BS341", "01/01/2026", "DAC", "DXB", 65),
    ])
    assert lookup.find("BS999", "01/01/2026") is None


# ---------------------------------------------------------------------------
# Audit integration
# ---------------------------------------------------------------------------


def _evt(
    pnr, ts_str, rbd, flight="BS341", date="01/01/2026",
    orig="DAC", dest="DXB", agent_id="agent1", passenger="PAX X",
) -> HistoryEvent:
    from datetime import datetime
    return HistoryEvent(
        source_file="t.xls", row_index=0,
        raw_date=ts_str, raw_created_by=f"Agent ({agent_id})",
        raw_description="", event_type="Ticket Modification",
        pnr=pnr, customer="",
        raw_flight=f"{flight} {orig} {dest} {date} 00:00",
        passenger=passenger,
        timestamp=datetime.strptime(ts_str, "%d/%m/%Y %H:%M"),
        agent=Agent(raw="Agent (agent1)", display_name="Agent",
                    user_id=agent_id, department=""),
        flight=FlightRef(raw="", flight_number=flight,
                        origin=orig, destination=dest,
                        flight_date=date, departure_time="00:00"),
        rbd_class=rbd,
    )


def test_downgrade_justification_uses_load_lookup():
    events = [
        _evt("AAA01", "01/01/2026 10:00", "Y", flight="BS341"),
        _evt("AAA01", "01/01/2026 14:00", "G", flight="BS341"),  # Y → G downgrade
        _evt("BBB02", "01/01/2026 10:30", "Y", flight="BS342"),
        _evt("BBB02", "01/01/2026 15:00", "G", flight="BS342"),  # also Y → G
    ]
    # BS341 was 95% full (questionable to downgrade) — BS342 was 30% (justified)
    lookup = LoadLookup.from_entries([
        _entry("BS341", "01/01/2026", "DAC", "DXB", 95),
        _entry("BS342", "01/01/2026", "DAC", "DXB", 30),
    ])
    rows = audit_downgrade_justification(events, lookup)
    by_pnr = {r.pnr: r for r in rows}
    assert by_pnr["AAA01"].verdict == VERDICT_QUESTIONABLE
    assert by_pnr["AAA01"].load_pct == 95
    assert by_pnr["BBB02"].verdict == VERDICT_JUSTIFIED
    assert by_pnr["BBB02"].load_pct == 30


def test_run_history_audit_includes_justifications_when_lookup_given():
    events = [
        _evt("AAA01", "01/01/2026 10:00", "Y", flight="BS341"),
        _evt("AAA01", "01/01/2026 14:00", "G", flight="BS341"),
    ]
    lookup = LoadLookup.from_entries([
        _entry("BS341", "01/01/2026", "DAC", "DXB", 95),
    ])
    report = run_history_audit(events, load_lookup=lookup)
    assert len(report.downgrade_justifications) == 1
    assert report.downgrade_justifications[0].verdict == VERDICT_QUESTIONABLE


def test_run_history_audit_without_lookup_has_empty_justifications():
    events = [
        _evt("AAA01", "01/01/2026 10:00", "Y", flight="BS341"),
        _evt("AAA01", "01/01/2026 14:00", "G", flight="BS341"),
    ]
    report = run_history_audit(events, load_lookup=None)
    assert report.downgrade_justifications == []
