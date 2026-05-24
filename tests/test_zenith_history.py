"""Tests for the Zenith Flight History parser + analyzer.

The HTML fixture is built inline with fully fake passenger/PNR data so
no real customer info ever lives in the repo.
"""

from __future__ import annotations

import textwrap
from datetime import datetime

import pytest

from src.zenith_history_analyzer import (
    audit_class_trajectories,
    audit_downgrade_leaderboard,
    audit_g_class_issuance,
    audit_suspicious,
    run_history_audit,
)
from src.zenith_history_parser import (
    extract_description_fields,
    is_downgrade,
    downgrade_severity,
    parse_agent,
    parse_flight,
    parse_history_file,
    parse_timestamp,
)


# ---------------------------------------------------------------------------
# Cell-level extractors
# ---------------------------------------------------------------------------


class TestParseAgent:
    def test_simple_name_and_user(self) -> None:
        a = parse_agent("Display Name (userid01)")
        assert a.display_name == "Display Name"
        assert a.user_id == "userid01"
        assert a.department == ""
        assert not a.is_api and not a.is_system

    def test_with_department(self) -> None:
        a = parse_agent("First Last (user123/BO-3 Revenue Management )")
        assert a.user_id == "user123"
        assert a.department == "BO-3 Revenue Management"

    def test_api_flag(self) -> None:
        a = parse_agent("api_agency api_agency (api_agency)")
        assert a.is_api is True

    def test_system_flag(self) -> None:
        a = parse_agent("System (/TTI)")
        assert a.is_system is True

    def test_malformed_falls_back_safely(self) -> None:
        a = parse_agent("Just a string with no parens")
        assert a.display_name == "Just a string with no parens"
        assert a.user_id == ""


class TestParseFlight:
    def test_canonical_format(self) -> None:
        f = parse_flight("BS 341 DAC CGP 01/01/2026 20:45")
        assert f.flight_number == "BS341"
        assert f.origin == "DAC"
        assert f.destination == "CGP"
        assert f.flight_date == "01/01/2026"
        assert f.departure_time == "20:45"

    def test_empty_returns_blanks(self) -> None:
        f = parse_flight("")
        assert f.flight_number == "" and f.origin == ""

    def test_unparseable_keeps_raw(self) -> None:
        f = parse_flight("not a real flight cell")
        assert f.flight_number == "" and f.raw == "not a real flight cell"


class TestParseTimestamp:
    def test_full_dd_mm_yyyy_hh_mm(self) -> None:
        ts = parse_timestamp("11/04/2026 10:45")
        assert ts == datetime(2026, 4, 11, 10, 45)

    def test_returns_none_on_bad_input(self) -> None:
        assert parse_timestamp("garbage") is None
        assert parse_timestamp("") is None


class TestExtractDescription:
    def test_rbd_from_coupon_info(self) -> None:
        d = "Coupon information: [12345] 0000000000000C1 BS 341 /G DAC CGP 01/01/2026"
        f = extract_description_fields(d)
        assert f["rbd_class"] == "G"

    def test_status_transition_arrow(self) -> None:
        d = "Coupon information: [...] BS 341 /E DAC CGP | Coupon status :Issued->Refunded"
        f = extract_description_fields(d)
        assert f["old_status"] == "Issued"
        assert f["new_status"] == "Refunded"

    def test_status_transition_equals_form(self) -> None:
        d = "Boarding of the coupon 0000 : Previous status = CK , New status = BD"
        f = extract_description_fields(d)
        assert f["old_status"] == "CK"
        assert f["new_status"] == "BD"

    def test_capacity_change(self) -> None:
        d = "Flight Capacity change: (CGP -DXB ) Cabine Eco (Booking class: H) 12 -> 0"
        f = extract_description_fields(d)
        assert f["capacity_class"] == "H"
        assert f["capacity_before"] == 12
        assert f["capacity_after"] == 0


class TestDowngradeMath:
    def test_y_to_g_is_downgrade(self) -> None:
        assert is_downgrade("Y", "G") is True

    def test_same_class_is_not_downgrade(self) -> None:
        assert is_downgrade("Y", "Y") is False

    def test_upgrade_is_not_downgrade(self) -> None:
        assert is_downgrade("G", "Y") is False

    def test_unknown_class_never_counts(self) -> None:
        assert is_downgrade("Y", "Z") is False
        assert is_downgrade("Z", "Y") is False

    def test_severity_counts_tier_drop(self) -> None:
        # Y rank=0, G rank=8 → 8-tier drop
        assert downgrade_severity("Y", "G") == 8
        assert downgrade_severity("Y", "Y") == 0


# ---------------------------------------------------------------------------
# File-level parsing
# ---------------------------------------------------------------------------


def _row(
    date: str, by: str, desc: str, etype: str,
    pnr: str, flight: str, passenger: str, customer: str = "",
) -> str:
    return f"""
    <tr>
      <td>{date}</td>
      <td>{by}</td>
      <td>{desc}</td>
      <td>{etype}</td>
      <td>{pnr}</td>
      <td>{customer}</td>
      <td>{flight}</td>
      <td>{passenger}</td>
    </tr>
    """


def _fixture_html(*rows: str) -> str:
    return textwrap.dedent(f"""
    <html><body><table>
      <thead><tr>
        <th>Date</th><th>Created by</th><th>Description</th><th>Type</th>
        <th>PNR</th><th>Customer</th><th>Flight</th><th>Passenger</th>
      </tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table></body></html>
    """).strip()


def test_parse_history_file_round_trip(tmp_path):
    html = _fixture_html(
        _row(
            "01/01/2026 10:00", "Tester One (tester1)",
            "Coupon information: [11111] 7792000000001C1 BS 999 /Y AAA BBB 01/01/2026 | Coupon status :Issued->Issued",
            "Ticket Modification",
            "TEST01", "BS 999 AAA BBB 01/01/2026 10:00", "TESTPAX ONE",
        ),
        _row(
            "01/01/2026 11:00", "Tester Two (tester2/BO-3 Test Dept)",
            "Coupon information: [11112] 7792000000002C1 BS 999 /G AAA BBB 01/01/2026 | Coupon status :Issued->Refunded",
            "Ticket Modification",
            "TEST01", "BS 999 AAA BBB 01/01/2026 10:00", "TESTPAX ONE",
        ),
    )
    p = tmp_path / "ModificationHistory test.xls"
    p.write_bytes(html.encode("utf-8"))
    events = parse_history_file(p)
    assert len(events) == 2
    assert events[0].agent.user_id == "tester1"
    assert events[0].rbd_class == "Y"
    assert events[1].rbd_class == "G"
    assert events[1].agent.department == "BO-3 Test Dept"
    assert events[1].old_status == "Issued"
    assert events[1].new_status == "Refunded"


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------


def _make_events(tmp_path) -> list:
    """Build a small but interesting event stream end-to-end."""
    html = _fixture_html(
        # PNR ALPHA01 booked Y → downgraded to G by tester1
        _row(
            "01/01/2026 09:00", "Tester One (tester1)",
            "Coupon information: [10001] 7792000000001C1 BS 100 /Y DAC DXB 01/01/2026 | Coupon status :Option->Issued",
            "Ticket Modification",
            "ALPHA01", "BS 100 DAC DXB 01/01/2026 19:00", "PAX ALPHA",
        ),
        _row(
            "01/01/2026 14:00", "Tester One (tester1)",
            "Coupon information: [10001] 7792000000001C1 BS 100 /G DAC DXB 01/01/2026 | Coupon status :Issued->Issued",
            "Ticket Modification",
            "ALPHA01", "BS 100 DAC DXB 01/01/2026 19:00", "PAX ALPHA",
        ),
        # PNR BRAVO02 booked T → stayed at T (no downgrade)
        _row(
            "01/01/2026 09:30", "Tester Two (tester2)",
            "Coupon information: [10002] 7792000000002C1 BS 100 /T DAC DXB 01/01/2026 | Coupon status :Option->Issued",
            "Ticket Modification",
            "BRAVO02", "BS 100 DAC DXB 01/01/2026 19:00", "PAX BRAVO",
        ),
        # Off-hours downgrade by tester3 on PNR CHARLIE03
        _row(
            "01/01/2026 09:30", "Tester Three (tester3)",
            "Coupon information: [10003] 7792000000003C1 BS 100 /Y DAC DXB 01/01/2026 | Coupon status :Option->Issued",
            "Ticket Modification",
            "CHARLIE03", "BS 100 DAC DXB 01/01/2026 19:00", "PAX CHARLIE",
        ),
        _row(
            "02/01/2026 02:15", "Tester Three (tester3)",
            "Coupon information: [10003] 7792000000003C1 BS 100 /M DAC DXB 01/01/2026 | Coupon status :Issued->Issued",
            "Ticket Modification",
            "CHARLIE03", "BS 100 DAC DXB 01/01/2026 19:00", "PAX CHARLIE",
        ),
        # Revenue mgmt capacity change
        _row(
            "01/01/2026 12:00", "RevMgr (revmgr1/BO-3 Revenue Management )",
            "Flight Capacity change: (DAC -DXB ) Cabine Eco (Booking class: H) 12 -> 0",
            "Class of service modification",
            "", "", "",
        ),
    )
    p = tmp_path / "ModificationHistory synthetic.xls"
    p.write_bytes(html.encode("utf-8"))
    return parse_history_file(p)


def test_run_history_audit_full(tmp_path):
    events = _make_events(tmp_path)
    report = run_history_audit(events)
    assert report.event_count == 6
    assert report.file_count == 1

    # ALPHA01 and CHARLIE03 are downgraded; BRAVO02 is not
    by_pnr = {t.pnr: t for t in report.class_trajectories}
    assert by_pnr["ALPHA01"].total_downgrade_severity == downgrade_severity("Y", "G")
    assert by_pnr["BRAVO02"].total_downgrade_severity == 0
    assert by_pnr["CHARLIE03"].total_downgrade_severity == downgrade_severity("Y", "M")

    # Downgrade leaders includes tester1 and tester3
    leader_uids = {l.agent_user_id for l in report.downgrade_leaders}
    assert "tester1" in leader_uids
    assert "tester3" in leader_uids
    assert "tester2" not in leader_uids

    # G-class issuance picks up the ALPHA01 G modification
    g_pnrs = [g.pnr for g in report.g_class_events]
    assert "ALPHA01" in g_pnrs

    # Revenue mgmt picks up the capacity change
    assert len(report.revenue_mgmt_changes) == 1
    rm = report.revenue_mgmt_changes[0]
    assert rm.booking_class == "H"
    assert rm.delta == -12

    # Off-hours flag fires on CHARLIE03 02:15 downgrade
    reasons = [f.reason for f in report.suspicious_flags]
    assert any("Off-hours" in r for r in reasons)


def test_empty_events_returns_empty_report():
    report = run_history_audit([])
    assert report.event_count == 0
    assert report.class_trajectories == []
    assert report.suspicious_flags == []
