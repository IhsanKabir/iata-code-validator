"""Regression tests for the flight-loads pagination + auto-split fix.

These reproduce the production bug where whole days went missing from a
range pull (Jun 4–18 dropped, Jun 3 & 10 partial) and prove the permanent
fix: exhaustion-based stop + cross-page dedup + auto-split of any chunk
that hits the server's 10-page cap.

Everything is synthetic — a fake session serves canned HTML keyed by
(date range, Nav page). No network, no real PNL data.
"""

from __future__ import annotations

import re

import pytest

from src import zenith_client
from src.zenith_client import (
    _log_flight_load_completeness,
    _split_date_range,
    fetch_flight_loads,
)


# ---------------------------------------------------------------------------
# Minimal parser-valid HTML builders (date-aware, multi-leg capable)
# ---------------------------------------------------------------------------


def _flight_block(flight: str, date: str, legs: list[tuple]) -> str:
    """legs: list of (route, avail). One block = one flight on one date.

    Mirrors the real Zenith markup closely enough for parse_flight_loads_html.
    NOTE: the parser's leg/billets/seats table regexes require a
    DIGITS-ONLY data-id-vol (\\d+), so the id must contain no letters.
    """
    flight_num = re.sub(r"\D", "", flight)        # 'BS101' -> '101'
    id_vol = f"{flight_num}{date.replace('/', '')}"  # all digits
    header = (
        f'<b>{flight}      - '
        f'<font color="white">Mon {date}&nbsp;07:00'
        f'<font class="FNTListRow">&nbsp;ATR-72 - S2-AKJ</font></b>'
        f' - <b>70 Tickets issued</b></font>'
    )
    parts = [header]
    for i, (route, _avail) in enumerate(legs):
        parts.append(
            f'<table data-id-vol="{id_vol}_{i}">'
            f'<tr><td class="inventorystatus" title=AS-open>x</td></tr></table>'
        )
    parts.append(f'<table data-table-leg data-id-vol="{id_vol}">')
    for route, _avail in legs:
        parts.append(
            f'<tr class="info"><td><u>{route}</u></td>'
            f'<td class="TDListRow heure"><font color="blue">{date} 07:00 - 08:05</font></td>'
            f'<td class="TDListRow stock">Cabin<nobr>Economy</nobr></td></tr>'
        )
    parts.append("</table>")
    parts.append(f'<table data-table-billets data-id-vol="{id_vol}">')
    for _route, _avail in legs:
        parts.append(
            '<tr class="billets"><td class="TDListRow emis"><div>Issued</div>'
            '<font class="FNTListRow">70(0)</font></td>'
            '<td class="TDListRow emis-wl"><div>WL</div>'
            '<font class="FNTListRow">0(0)</font></td></tr>'
        )
    parts.append("</table>")
    parts.append(f'<table data-table-zs data-id-vol="{id_vol}">')
    for _route, avail in legs:
        parts.append(
            '<tr class="sieges"><td class="TDListRow confirm"><div>C</div>'
            '<font class="FNTListRow">[70]</font></td>'
            '<td class="TDListRow options"><div>O</div>'
            '<font class="FNTListRow">0(0)</font></td>'
            '<td class="TDListRow wl"><div>W</div>'
            '<font class="FNTListRow">[0]</font></td>'
            '<td class="TDListRow td-dispo reste"><div>A</div>'
            f'<font class="FNTListRow"><font color="red">{avail}</font></td></tr>'
        )
    parts.append("</table>")
    return "".join(parts)


def _page_html(flight_specs: list[tuple]) -> str:
    """flight_specs: list of (flight, date, legs). Empty list → no flights."""
    if not flight_specs:
        return "<html><body>no flights</body></html>"
    return "<html><body>" + "".join(
        _flight_block(f, d, legs) for (f, d, legs) in flight_specs
    ) + "</body></html>"


# ---------------------------------------------------------------------------
# Fake session
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, text: str, url: str) -> None:
        self.text = text
        self.url = url
        self.status_code = 200

    def raise_for_status(self) -> None:
        pass


class _FakeReqSession:
    def __init__(self, handler) -> None:
        self._handler = handler
        self.headers: dict = {}
        self.calls: list[tuple[str, str, int]] = []  # (cfrom, cto, page)

    def post(self, url, data=None, timeout=None, allow_redirects=True):
        page = int(re.search(r"Nav=(\d+)", url).group(1))
        cfrom = data["date_depart_vol"]
        cto = data["date_fin_vol"]
        self.calls.append((cfrom, cto, page))
        return _FakeResp(self._handler(cfrom, cto, page), url)


class _FakeZenith:
    def __init__(self, handler) -> None:
        self.session = _FakeReqSession(handler)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_does_not_stop_early_on_multistop_full_page():
    """THE BUG: a full page whose distinct-flight count is below page_size
    (because of multi-stop flights) must NOT be treated as the last page.

    page_size=4. Page 1 = 3 flights, one multi-stop (2 legs) = 4 rows but
    only 3 distinct flights. The OLD code did `if distinct < page_size: break`
    → stopped after page 1 → dropped page 2's flights. The fix continues.
    """
    def handler(cfrom, cto, page):
        if page == 1:
            return _page_html([
                ("BS101", "03/06/2026", [("DAC-CGP", "2/72 97%"), ("CGP-DAC", "5/72 93%")]),
                ("BS103", "03/06/2026", [("DAC-JSR", "1/72 99%")]),
                ("BS105", "03/06/2026", [("DAC-SPD", "3/72 96%")]),
            ])
        if page == 2:
            return _page_html([
                ("BS107", "03/06/2026", [("DAC-RJH", "4/72 94%")]),
                ("BS109", "03/06/2026", [("DAC-BZL", "6/72 91%")]),
            ])
        return _page_html([])  # page 3 empty → exhausted

    sess = _FakeZenith(handler)
    rows = fetch_flight_loads(
        sess, "03/06/2026", "03/06/2026",
        page_size=4, chunk_days=1, inter_call_delay_s=0,
    )
    flights = {r.flight_number for r in rows}
    # Page-2 flights MUST be present — the old heuristic dropped them.
    assert "BS107" in flights
    assert "BS109" in flights
    # Multi-stop legs preserved.
    assert sum(1 for r in rows if r.flight_number == "BS101") == 2


def test_auto_split_recovers_dates_beyond_page_cap():
    """A chunk that hits the 10-page cap with data still arriving must be
    split and re-fetched so late dates are never silently dropped.

    The full range serves 10 full pages of EARLY dates only (mirroring the
    real bug where the back half of a chunk never came back). The fix
    detects the cap-hit, splits, and the second half returns the late dates.
    """
    full_range = ("01/06/2026", "10/06/2026")

    def handler(cfrom, cto, page):
        if (cfrom, cto) == full_range:
            # 10 pages, each 2 new early-date flights → hits cap with data,
            # and NEVER serves 06–10 June (exactly the production symptom).
            if 1 <= page <= 10:
                n = page * 2
                return _page_html([
                    (f"BS{100 + n}", "01/06/2026", [("DAC-CGP", "2/72 97%")]),
                    (f"BS{101 + n}", "02/06/2026", [("DAC-JSR", "1/72 99%")]),
                ])
            return _page_html([])
        # After split: each half serves its own dates then exhausts.
        from datetime import datetime
        d_from = datetime.strptime(cfrom, "%d/%m/%Y").date()
        d_to = datetime.strptime(cto, "%d/%m/%Y").date()
        if page == 1:
            specs = []
            cur = d_from
            while cur <= d_to:
                ds = cur.strftime("%d/%m/%Y")
                specs.append((f"BS2{cur.day:02d}", ds, [("DAC-DXB", "3/72 96%")]))
                cur = cur.fromordinal(cur.toordinal() + 1)
            return _page_html(specs)
        return _page_html([])

    sess = _FakeZenith(handler)
    rows = fetch_flight_loads(
        sess, full_range[0], full_range[1],
        page_size=2, chunk_days=10, inter_call_delay_s=0,
    )
    dates = {r.flight_date for r in rows}
    # The whole point: 06–10 June must be recovered via the split.
    for d in ("06/06/2026", "07/06/2026", "08/06/2026", "09/06/2026", "10/06/2026"):
        assert d in dates, f"{d} missing — auto-split failed to recover it"
    # And the original chunk was indeed split (re-fetched sub-ranges).
    sub_ranges = {(c, t) for (c, t, _p) in sess.session.calls}
    assert any(r != full_range for r in sub_ranges), "expected a split re-fetch"


def test_broken_nav_server_stops_via_dedup():
    """If the server ignores Nav and re-serves page 1 forever, the dedup
    stop must halt after the first all-duplicate page — no infinite loop,
    no duplicate rows.
    """
    same_page = [
        ("BS101", "03/06/2026", [("DAC-CGP", "2/72 97%")]),
        ("BS103", "03/06/2026", [("DAC-JSR", "1/72 99%")]),
    ]

    def handler(cfrom, cto, page):
        return _page_html(same_page)  # every Nav returns the same data

    sess = _FakeZenith(handler)
    rows = fetch_flight_loads(
        sess, "03/06/2026", "03/06/2026",
        page_size=2, chunk_days=1, inter_call_delay_s=0,
    )
    assert len(rows) == 2  # deduped, not 2×N pages
    # Stopped after page 2 (page 1 = new, page 2 = all dupes → stop).
    pages_fetched = [p for (_c, _t, p) in sess.session.calls]
    assert max(pages_fetched) == 2


def test_empty_first_page_stops_immediately():
    def handler(cfrom, cto, page):
        return _page_html([])

    sess = _FakeZenith(handler)
    rows = fetch_flight_loads(
        sess, "03/06/2026", "03/06/2026",
        page_size=2, chunk_days=1, inter_call_delay_s=0,
    )
    assert rows == []


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_split_date_range_halves_inclusively():
    lo, hi = _split_date_range("01/06/2026", "10/06/2026")
    assert lo == ("01/06/2026", "05/06/2026")
    assert hi == ("06/06/2026", "10/06/2026")


def test_split_two_day_range():
    lo, hi = _split_date_range("01/06/2026", "02/06/2026")
    assert lo == ("01/06/2026", "01/06/2026")
    assert hi == ("02/06/2026", "02/06/2026")


def test_completeness_flags_missing_dates():
    class _Row:
        def __init__(self, d):
            self.flight_date = d
    rows = [_Row("01/06/2026"), _Row("01/06/2026"), _Row("03/06/2026")]
    missing = _log_flight_load_completeness(rows, "01/06/2026", "04/06/2026")
    assert missing == ["02/06/2026", "04/06/2026"]


def test_completeness_no_gaps():
    class _Row:
        def __init__(self, d):
            self.flight_date = d
    rows = [_Row("01/06/2026"), _Row("02/06/2026")]
    assert _log_flight_load_completeness(rows, "01/06/2026", "02/06/2026") == []


# ---------------------------------------------------------------------------
# Leg classification (Domestic/International + Inbound/Outbound)
# ---------------------------------------------------------------------------

from src.zenith_client import classify_leg_region, classify_leg_direction


import pytest as _pytest


@_pytest.mark.parametrize(("o", "d", "region"), [
    ("DAC", "CXB", "Domestic"),
    ("ZYL", "DAC", "Domestic"),
    ("CGP", "DAC", "Domestic"),
    ("DAC", "KUL", "International"),
    ("DAC", "MLE", "International"),
    ("DXB", "DAC", "International"),
    ("CGP", "MCT", "International"),
])
def test_classify_leg_region(o, d, region):
    assert classify_leg_region(o, d) == region


@_pytest.mark.parametrize(("o", "d", "direction"), [
    ("DAC", "CXB", "Outbound"),   # domestic ex-hub
    ("ZYL", "DAC", "Inbound"),    # domestic to-hub
    ("DAC", "DXB", "Outbound"),   # leaving BD
    ("DXB", "DAC", "Inbound"),    # entering BD
    ("RJH", "DAC", "Inbound"),
    ("DAC", "MLE", "Outbound"),
    ("MCT", "CGP", "Inbound"),    # foreign->BD
    ("CGP", "MCT", "Outbound"),   # BD->foreign
])
def test_classify_leg_direction(o, d, direction):
    assert classify_leg_direction(o, d) == direction
