"""Tests for the Zenith PNR client + cache + analyzer enrichment.

All fixtures are inline with fully fake passenger/PNR data so no real
booking information lives in the repo. The HTML structure mirrors the
ASP.NET WebForms field-naming used by Dossier.aspx.
"""

from __future__ import annotations

import textwrap
from datetime import datetime

import pytest

from src.zenith_history_analyzer import (
    apply_pnr_enrichment,
    build_pnr_routes,
    run_history_audit,
)
from src.zenith_history_parser import Agent, FlightRef, HistoryEvent
from src.zenith_pnr_cache import ZenithPNRCache
from src.zenith_pnr_client import (
    PNRDetails,
    PNRNotFoundError,
    PNRSegment,
    parse_dossier_html,
)


# ---------------------------------------------------------------------------
# Synthetic Dossier HTML builder
# ---------------------------------------------------------------------------


def _lbl(field: str, value: str) -> str:
    """Render an ASP.NET WebForms label span the parser anchors on."""
    return (
        f'<span id="instanceCtrlContent_'
        f'UsrDossierSynthese_rptSegments_ctl00_rptVols_ctl00_lbl{field}">'
        f"{value}</span>"
    )


def _seg_lbl(seg_idx: int, field: str, value: str) -> str:
    """Per-passenger / per-segment label (rptPassagers under rptVols)."""
    return (
        f'<span id="instanceCtrlContent_'
        f'UsrDossierSynthese_rptSegments_ctl00_rptVols_ctl00_'
        f'rptPassagers_ctl0{seg_idx}_lbl{field}">{value}</span>'
    )


def _etat(seg_idx: int, status: str) -> str:
    return (
        f'<a id="instanceCtrlContent_'
        f'UsrDossierSynthese_rptSegments_ctl00_rptVols_ctl00_'
        f'rptPassagers_ctl0{seg_idx}_hlEtat">{status}</a>'
    )


def _pax(seg_idx: int, name: str) -> str:
    return (
        f'<a id="instanceCtrlContent_'
        f'UsrDossierSynthese_rptSegments_ctl00_rptVols_ctl00_'
        f'rptPassagers_ctl0{seg_idx}_linkPassager">{name}</a>'
    )


def _build_dossier_html(
    pnr: str = "TEST01",
    dossier_id: str = "99999999",
    customer: str = "Test Customer Ltd",
    surname: str = "TESTSURNAME",
    phone: str = "+8801000000000",
    out_route: str = "DAC - DXB",
    return_route: str = "DXB - DAC",
    out_status: str = "Issued",
    return_status: str = "Issued",
    out_fare: str = "20,000 BDT",
    return_fare: str = "22,000 BDT",
    pax_count: str = "1 pax",
) -> str:
    parts = [
        "<html><body>",
        f"PNR : {dossier_id} | {pnr} - {customer}",
        _lbl("PNRCode", pnr),
        _lbl("CustomerName", customer),
        _lbl("NomProprio", surname),
        _lbl("TelMobile", phone),
        _lbl("NbPaxForPNR", pax_count),
        _lbl("EtatDossier", "Issued"),
        _lbl("Paiement", "On account"),
        _lbl("DeviseDossier", "BDT"),
        _lbl("PrixTotalTTC", "42,000 BDT"),
        _lbl("TaxesTotal", "2,000 BDT"),
        _lbl("LegAller", out_route),
        _lbl("LegRetour", return_route),
        _lbl("DatesAller", "Departure Tue 01/01/26"),
        _lbl("DatesRetour", "Departure Sun 07/01/26"),
        _seg_lbl(1, "DepartVol", "From DAC 22:30"),
        _seg_lbl(1, "ArriveeVol", "- To DXB 04:00+1"),
        _seg_lbl(1, "AircraftNumber", "Boeing 737-800 (S2-AJE)"),
        _seg_lbl(1, "Classe", "GTESTR6M-AD"),
        _seg_lbl(1, "PrixHT", "18,000 BDT"),
        _seg_lbl(1, "PrixTTC", out_fare),
        _seg_lbl(1, "TicketNumber", "7792000000001 6/1"),
        _pax(1, surname),
        _etat(1, out_status),
        _seg_lbl(2, "DepartVol", "From DXB 09:00"),
        _seg_lbl(2, "ArriveeVol", "- To DAC 19:00"),
        _seg_lbl(2, "AircraftNumber", "Boeing 737-800 (S2-AJF)"),
        _seg_lbl(2, "Classe", "MTESTRT-AD"),
        _seg_lbl(2, "PrixHT", "20,000 BDT"),
        _seg_lbl(2, "PrixTTC", return_fare),
        _seg_lbl(2, "TicketNumber", "7792000000001 6/2"),
        _pax(2, surname),
        _etat(2, return_status),
        "</body></html>",
    ]
    return "".join(parts)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class TestParseDossier:
    def test_extracts_basic_pnr_metadata(self) -> None:
        d = parse_dossier_html(_build_dossier_html())
        assert d.pnr_code == "TEST01"
        assert d.customer_name == "Test Customer Ltd"
        assert d.traveler_surname == "TESTSURNAME"
        assert d.phone == "+8801000000000"
        assert d.pax_count == 1
        assert d.pnr_status == "Issued"
        assert d.currency == "BDT"

    def test_extracts_segments_with_status(self) -> None:
        d = parse_dossier_html(_build_dossier_html(
            out_status="Flown", return_status="Refunded",
        ))
        assert len(d.segments) == 2
        out, ret = d.segments
        assert out.leg_route == "DAC-DXB"
        assert out.leg_direction == "OUT"
        assert out.coupon_status == "Flown"
        assert out.rbd_class == "G"
        assert ret.leg_route == "DXB-DAC"
        assert ret.leg_direction == "RETURN"
        assert ret.coupon_status == "Refunded"
        assert ret.rbd_class == "M"

    def test_booked_route_collapses_repeats(self) -> None:
        d = parse_dossier_html(_build_dossier_html())
        assert d.booked_route == "DAC-DXB-DAC"

    def test_flown_route_drops_refunded_segments(self) -> None:
        d = parse_dossier_html(_build_dossier_html(
            out_status="Flown", return_status="Refunded",
        ))
        # Outbound flown, return refunded → flown route ends at DXB.
        assert d.flown_route == "DAC-DXB"

    def test_flown_route_empty_when_everything_voided(self) -> None:
        d = parse_dossier_html(_build_dossier_html(
            out_status="Voided", return_status="Voided",
        ))
        assert d.flown_route == ""
        # But booked route stays intact.
        assert d.booked_route == "DAC-DXB-DAC"

    def test_raises_when_no_pnr_field_present(self) -> None:
        with pytest.raises(PNRNotFoundError):
            parse_dossier_html("<html><body>nothing</body></html>")


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class TestCache:
    def _details(self, pnr: str = "TEST99") -> PNRDetails:
        return parse_dossier_html(_build_dossier_html(pnr=pnr))

    def test_round_trip(self, tmp_path) -> None:
        cache = ZenithPNRCache(tmp_path / "p.sqlite")
        d = self._details()
        cache.put(d)
        got = cache.get("TEST99")
        assert got is not None
        assert got.pnr_code == "TEST99"
        assert len(got.segments) == 2
        assert got.segments[0].coupon_status == "Issued"

    def test_case_insensitive_lookup(self, tmp_path) -> None:
        cache = ZenithPNRCache(tmp_path / "p.sqlite")
        cache.put(self._details("UpPeR1"))
        # Stored uppercase, retrievable case-insensitive.
        assert cache.get("upper1") is not None

    def test_put_many_then_count(self, tmp_path) -> None:
        cache = ZenithPNRCache(tmp_path / "p.sqlite")
        cache.put_many([self._details(f"PNR{i:03d}") for i in range(5)])
        assert cache.count() == 5


# ---------------------------------------------------------------------------
# Analyzer enrichment
# ---------------------------------------------------------------------------


def _evt(pnr: str, ts: str, rbd: str) -> HistoryEvent:
    return HistoryEvent(
        source_file="t.xls", row_index=0,
        raw_date=ts, raw_created_by="A (a)",
        raw_description="", event_type="Ticket Modification",
        pnr=pnr, customer="", raw_flight="BS999 DAC DXB 01/01/2026 00:00",
        passenger="PAX",
        timestamp=datetime.strptime(ts, "%d/%m/%Y %H:%M"),
        agent=Agent(raw="A (a)", display_name="A", user_id="a", department=""),
        flight=FlightRef(raw="", flight_number="BS999",
                        origin="DAC", destination="DXB",
                        flight_date="01/01/2026", departure_time="00:00"),
        rbd_class=rbd,
    )


def test_build_pnr_routes_orders_disrupted_first() -> None:
    """PNRs with refund/void activity should sort above clean PNRs."""
    clean = parse_dossier_html(_build_dossier_html(pnr="CLEAN1"))
    disrupted = parse_dossier_html(_build_dossier_html(
        pnr="DISRP1", out_status="Flown", return_status="Refunded",
    ))
    rows = build_pnr_routes({"CLEAN1": clean, "DISRP1": disrupted})
    assert rows[0].pnr_code == "DISRP1"
    assert rows[0].refunded_count == 1
    assert rows[1].pnr_code == "CLEAN1"


def test_apply_pnr_enrichment_fills_customer_name() -> None:
    events = [
        _evt("PNR001", "01/01/2026 10:00", "Y"),
        _evt("PNR001", "01/01/2026 14:00", "G"),
    ]
    report = run_history_audit(events)
    # Pre-enrichment: customer_name is empty on the trajectory.
    assert report.class_trajectories[0].customer_name == ""

    details = parse_dossier_html(_build_dossier_html(
        pnr="PNR001", customer="Acme Travels",
    ))
    enriched = apply_pnr_enrichment(report, {"PNR001": details})
    assert enriched.class_trajectories[0].customer_name == "Acme Travels"
    assert len(enriched.pnr_routes) == 1
    assert enriched.pnr_routes[0].customer_name == "Acme Travels"


def test_read_pnr_codes_from_excel_uses_named_column(tmp_path) -> None:
    from openpyxl import Workbook
    from src.excel_io import read_pnr_codes_from_excel

    p = tmp_path / "input.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Note", "PNR", "Other"])
    ws.append(["x", "abc123", "y"])
    ws.append(["x", None, "y"])
    ws.append(["x", "  xyz999  ", "y"])
    wb.save(p)
    codes = read_pnr_codes_from_excel(p, column_name="PNR")
    assert codes == ["ABC123", "XYZ999"]


def test_read_pnr_codes_from_excel_defaults_to_first_column(tmp_path) -> None:
    from openpyxl import Workbook
    from src.excel_io import read_pnr_codes_from_excel

    p = tmp_path / "input.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Anything"])
    for code in ["aaa111", "bbb222", "ccc333"]:
        ws.append([code])
    wb.save(p)
    assert read_pnr_codes_from_excel(p) == ["AAA111", "BBB222", "CCC333"]


def test_write_zenith_pnr_bulk_creates_both_sheets(tmp_path) -> None:
    from openpyxl import load_workbook
    from src.excel_io import write_zenith_pnr_bulk

    details = parse_dossier_html(_build_dossier_html(
        pnr="ABC123", out_status="Flown", return_status="Refunded",
    ))
    p = tmp_path / "out.xlsx"
    # MISSING has no entry in `errors` → writer flags it as NOT_FOUND
    # (the worker treats PNRNotFoundError as distinct from real errors).
    write_zenith_pnr_bulk(
        p,
        [("ABC123", details), ("MISSING", None)],
    )
    wb = load_workbook(p)
    assert wb.sheetnames == ["PNR Lookup", "Segments"]
    # First data row is the resolved PNR.
    lookup = wb["PNR Lookup"]
    rows = list(lookup.iter_rows(values_only=True))
    assert rows[0][0] == "PNR"  # header
    assert rows[1][0] == "ABC123"
    assert rows[1][2] == "Test Customer Ltd"
    assert rows[1][8] == "DAC-DXB"      # flown route — return refunded
    assert rows[1][-2] == "OK"          # lookup status
    # The unresolved PNR gets a NOT_FOUND status.
    assert rows[2][0] == "MISSING"
    assert rows[2][-2] == "NOT_FOUND"

    segments = wb["Segments"]
    seg_rows = list(segments.iter_rows(values_only=True))
    assert seg_rows[0][0] == "PNR"  # header
    # 2 segments for ABC123, 0 for MISSING (no details)
    assert len(seg_rows) == 1 + 2


def test_apply_pnr_enrichment_keeps_old_report_immutable() -> None:
    """Enrichment returns a NEW report — the original is not mutated."""
    events = [
        _evt("PNR002", "01/01/2026 10:00", "Y"),
        _evt("PNR002", "01/01/2026 14:00", "G"),
    ]
    report = run_history_audit(events)
    details = parse_dossier_html(_build_dossier_html(pnr="PNR002"))
    enriched = apply_pnr_enrichment(report, {"PNR002": details})
    assert enriched is not report
    # Original trajectory still has empty customer_name.
    assert report.class_trajectories[0].customer_name == ""
