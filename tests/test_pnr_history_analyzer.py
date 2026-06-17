"""Tests for src/zenith_pnr_history_analyzer.py — the PNR misuse audit re-pivoted from
the flight ModificationHistory corpus. All offline against synthetic HistoryEvents.
"""
from __future__ import annotations

from datetime import datetime

from src.zenith_history_parser import Agent, FlightRef, HistoryEvent
from src.zenith_pnr_history_analyzer import (
    classify_action,
    run_pnr_misuse_audit,
)


def _agent(uid="agt1", dept="DAC Sales", name="Agent One") -> Agent:
    return Agent(raw=f"{name} ({uid}/{dept})", display_name=name, user_id=uid, department=dept)


_FLIGHT = FlightRef(raw="BS341 DAC DXB", flight_number="BS341", origin="DAC",
                    destination="DXB", flight_date="15/06/2026", departure_time="20:45")


def _ev(*, pnr="ABCDEF", ticket="7792000000001", new_status="", old_status="", rbd="",
        hour=14, day=15, agent=None, etype="Ticket Modification", desc="evt") -> HistoryEvent:
    ts = datetime(2026, 6, day, hour, 0)
    return HistoryEvent(
        source_file="f.xls", row_index=1, raw_date=ts.strftime("%d/%m/%Y %H:%M"),
        raw_created_by=(agent or _agent()).raw, raw_description=desc, event_type=etype,
        pnr=pnr, customer="C", raw_flight="BS341 DAC DXB", passenger="P",
        timestamp=ts, agent=agent or _agent(), flight=_FLIGHT,
        rbd_class=rbd, old_status=old_status, new_status=new_status, ticket_number=ticket,
    )


def _detectors(report) -> set[str]:
    return {f.detector for f in report.flags}


class TestClassify:
    def test_actions(self) -> None:
        assert classify_action(_ev(new_status="Refunded")) == "refund"
        assert classify_action(_ev(new_status="Voided")) == "void"
        assert classify_action(_ev(new_status="Issued")) == "issue"
        assert classify_action(_ev(new_status="Flown")) == "flown"
        assert classify_action(_ev(new_status="")) == "modify"


class TestDetectors:
    def test_refund_of_flown_is_critical(self) -> None:
        evs = [
            _ev(new_status="Flown", hour=21),                 # coupon flown
            _ev(new_status="Refunded", hour=21, day=16),      # later refunded
        ]
        rep = run_pnr_misuse_audit(evs)
        crit = [f for f in rep.flags if f.detector == "refund_of_flown"]
        assert crit and crit[0].severity == "critical"

    def test_self_refund_segregation_of_duties(self) -> None:
        a = _agent(uid="same1")
        evs = [
            _ev(new_status="Issued", agent=a),
            _ev(new_status="Refunded", agent=a, day=16),
        ]
        rep = run_pnr_misuse_audit(evs)
        sod = [f for f in rep.flags if f.detector == "self_refund_sod"]
        assert sod and sod[0].agent_user_id == "same1"

    def test_off_hours_refund(self) -> None:
        rep = run_pnr_misuse_audit([_ev(new_status="Refunded", hour=2)])
        assert "off_hours_value" in _detectors(rep)

    def test_business_hours_refund_not_off_hours(self) -> None:
        rep = run_pnr_misuse_audit([_ev(new_status="Refunded", hour=14)])
        assert "off_hours_value" not in _detectors(rep)

    def test_downgrade_flagged_with_severity(self) -> None:
        evs = [
            _ev(rbd="Y", new_status="Issued"),
            _ev(rbd="G", hour=15),                            # Y(0) -> G(8) = steep
        ]
        rep = run_pnr_misuse_audit(evs)
        dg = [f for f in rep.flags if f.detector == "downgrade"]
        assert dg and dg[0].severity == "high"               # >=6 tiers

    def test_repeated_class_change_churn(self) -> None:
        evs = [
            _ev(rbd="Y", new_status="Issued"),
            _ev(rbd="H", hour=15), _ev(rbd="R", hour=16), _ev(rbd="N", hour=17),
        ]
        rep = run_pnr_misuse_audit(evs)
        assert "repeated_class_change" in _detectors(rep)

    def test_refund_void_burst_by_agent(self) -> None:
        a = _agent(uid="burst1")
        evs = [_ev(pnr=f"P{i:04d}", ticket=f"77920000000{i:02d}",
                   new_status="Refunded", agent=a, hour=14) for i in range(8)]
        rep = run_pnr_misuse_audit(evs)
        assert "refund_void_burst" in _detectors(rep)

    def test_system_and_api_logins_excluded(self) -> None:
        sysagent = Agent(raw="System (system)", display_name="System",
                         user_id="system", department="")
        evs = [
            _ev(new_status="Flown", hour=21, agent=sysagent),
            _ev(new_status="Refunded", hour=2, day=16, agent=sysagent),
        ]
        rep = run_pnr_misuse_audit(evs)
        assert rep.flags == ()                               # nothing flagged for system

    def test_clean_pnr_no_flags(self) -> None:
        rep = run_pnr_misuse_audit([_ev(new_status="Issued", hour=11)])
        assert rep.flags == ()


class TestRiskAndTrends:
    def test_corroborated_pnr_ranks_high(self) -> None:
        a = _agent(uid="multi1")
        # One PNR/ticket lit by THREE families: flown->refund (crit), self-refund (high),
        # off-hours (med) — should top the worklist over a single-family PNR.
        hot = [
            _ev(pnr="HOT001", new_status="Issued", agent=a, hour=2),
            _ev(pnr="HOT001", new_status="Flown", agent=a, hour=21, day=15),
            _ev(pnr="HOT001", new_status="Refunded", agent=a, hour=2, day=16),
        ]
        mild = [_ev(pnr="MILD01", ticket="7792999999999", new_status="Refunded", hour=2)]
        rep = run_pnr_misuse_audit(hot + mild)
        pnr_rows = [r for r in rep.risk_worklist if r.grain == "pnr"]
        assert pnr_rows[0].entity == "HOT001"
        assert len(pnr_rows[0].families) >= 2                # corroboration bonus applied

    def test_agent_activity_counts(self) -> None:
        a = _agent(uid="act1")
        evs = [
            _ev(pnr="P1", new_status="Issued", agent=a),
            _ev(pnr="P2", ticket="7792000000002", new_status="Refunded", agent=a),
            _ev(pnr="P3", ticket="7792000000003", new_status="Voided", agent=a),
        ]
        rep = run_pnr_misuse_audit(evs)
        row = next(r for r in rep.agent_activity if r.agent_user_id == "act1")
        assert row.refunds == 1 and row.voids == 1 and row.issues == 1
        assert row.distinct_pnrs == 3

    def test_report_summary_fields(self) -> None:
        rep = run_pnr_misuse_audit([_ev(new_status="Issued")])
        assert rep.event_count == 1 and rep.pnr_count == 1 and rep.agent_count == 1
        assert rep.date_range[0] is not None
