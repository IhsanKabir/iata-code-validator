"""Phase-2 dossier audit: parser + payment/contact detectors + (mocked) downloader.

All offline. The parser is validated against the Step-0 comment format; the downloader is
exercised with a fake ZenithSession + monkeypatched lookup_pnr (no live Zenith).
"""
from __future__ import annotations

from datetime import datetime

from src.zenith_history_parser import Agent
from src.zenith_pnr_history_analyzer import run_dossier_audit
from src.zenith_pnr_history_cache import ZenithPNRHistoryCache
from src.zenith_pnr_history_parser import DossierEvent, parse_dossier_changes


# --------------------------------------------------------------------------- parser
def _row(date, by, desc, typ="File Modification", pnr="09AHEA", cust="AGY", flight="", pax=""):
    return (f"<tr><td>{date}</td><td>{by}</td><td>{desc}</td><td>{typ}</td>"
            f"<td>{pnr}</td><td>{cust}</td><td>{flight}</td><td>{pax}</td></tr>")


def _changes_html(*rows: str) -> str:
    return ("<table><tr><th>Date</th><th>Created by</th><th>Description</th><th>Type</th>"
            "<th>PNR</th><th>Customer</th><th>Flight</th><th>Passenger</th></tr>"
            + "".join(rows) + "</table>")


_AGENT = "Chakrabarty Taposh (taposh2589/DAC-16 Banani New)"


class TestParser:
    def test_payment_and_contact_change(self) -> None:
        html = _changes_html(_row(
            "14/06/2026 07:50", _AGENT,
            "Comment: PAX CONTACT-01846465588 -&gt; PAX CONTACT-01846465599<br> "
            "BKASH PAYMENT//Transaction ID-DFE8B5SN4W//<br>"))
        evs = parse_dossier_changes(html, "15605650")
        assert len(evs) == 1
        e = evs[0]
        assert e.payment_method == "BKASH" and e.payment_txn_id == "DFE8B5SN4W"
        assert e.contact_old == "01846465588" and e.contact_new == "01846465599"
        assert e.contact_changed is True
        assert e.timestamp == datetime(2026, 6, 14, 7, 50)

    def test_initial_contact_set_is_not_a_change(self) -> None:
        html = _changes_html(_row("14/06/2026 07:48", _AGENT,
                                   "Comment:  -&gt; PAX CONTACT-01846465588<br>"))
        e = parse_dossier_changes(html, "D")[0]
        assert e.contact_old == "" and e.contact_new == "01846465588"
        assert e.contact_changed is False

    def test_no_op_resave_is_not_a_change(self) -> None:
        html = _changes_html(_row("14/06/2026 07:50", _AGENT,
                                  "Comment: PAX CONTACT-555 -&gt; PAX CONTACT-555<br>"))
        assert parse_dossier_changes(html, "D")[0].contact_changed is False

    def test_reissue_exchange_detected(self) -> None:
        html = _changes_html(_row("13/06/2026 10:00", _AGENT,
                                  "Issued-&gt;Exchanged<br>IATA Coupon status  :I  -&gt;E ",
                                  typ="Ticket Modification"))
        e = parse_dossier_changes(html, "D")[0]
        assert e.is_reissue is True and e.coupon_from == "I" and e.coupon_to == "E"

    def test_empty_or_error_html_yields_nothing(self) -> None:
        assert parse_dossier_changes("", "D") == []
        assert parse_dossier_changes("<html><body>504 Gateway Timeout</body></html>", "D") == []
        assert parse_dossier_changes("<table><tr><td>nope</td></tr></table>", "D") == []


# --------------------------------------------------------------------------- detectors
def _de(pnr, *, txn="", cold="", cnew="", uid="agt1", dept="DAC-02 Customer Service",
        day=1, reissue=False, desc="d"):
    return DossierEvent(
        dossier_id=f"D{pnr}", row_index=0, raw_date="", timestamp=datetime(2026, 6, day, 10, 0),
        agent=Agent(raw=f"X ({uid}/{dept})", display_name="X", user_id=uid, department=dept),
        raw_description=desc, event_type="File Modification", pnr=pnr, customer="C",
        raw_flight="", passenger="", payment_txn_id=txn, contact_old=cold, contact_new=cnew,
        is_reissue=reissue)


class TestDetectors:
    def test_payment_txn_reuse_across_pnrs(self) -> None:
        rep = run_dossier_audit([_de("P1", txn="TX1"), _de("P2", txn="TX1")])
        f = [x for x in rep.flags if x.detector == "payment_txn_reuse"]
        assert f and f[0].ticket_number == "TX1"

    def test_txn_reuse_multi_agent_is_critical(self) -> None:
        rep = run_dossier_audit([_de("P1", txn="TX1", uid="a"), _de("P2", txn="TX1", uid="b")])
        f = [x for x in rep.flags if x.detector == "payment_txn_reuse"][0]
        assert f.severity == "critical"

    def test_single_pnr_txn_not_flagged(self) -> None:
        rep = run_dossier_audit([_de("P1", txn="TX1"), _de("P1", txn="TX1")])
        assert not [x for x in rep.flags if x.detector == "payment_txn_reuse"]

    def test_contact_churn(self) -> None:
        evs = [_de("C", cold="1", cnew="2"), _de("C", cold="2", cnew="3"), _de("C", cold="3", cnew="4")]
        assert "contact_churn" in {f.detector for f in run_dossier_audit(evs).flags}

    def test_contact_funnel(self) -> None:
        evs = [_de(f"F{i}", cnew="0199999999") for i in range(5)]
        assert "contact_funnel" in {f.detector for f in run_dossier_audit(evs).flags}

    def test_clean_events_no_flags(self) -> None:
        assert run_dossier_audit([_de("P1", cnew="111"), _de("P2", cnew="222")]).flags == ()

    def test_system_actor_excluded(self) -> None:
        sysd = DossierEvent(
            dossier_id="D", row_index=0, raw_date="", timestamp=datetime(2026, 6, 1, 10, 0),
            agent=Agent(raw="System (/TTI)", display_name="System", user_id="", department=""),
            raw_description="d", event_type="t", pnr="P1", customer="", raw_flight="", passenger="",
            payment_txn_id="TX1")
        rep = run_dossier_audit([sysd, _de("P2", txn="TX1")])
        # only the human's PNR remains -> single-PNR txn -> no reuse flag
        assert not [f for f in rep.flags if f.detector == "payment_txn_reuse"]

    def test_coverage_counts(self) -> None:
        rep = run_dossier_audit([_de("P1", txn="TX1", cold="1", cnew="2")])
        assert rep.payments_seen == 1 and rep.contacts_changed == 1 and rep.distinct_txn == 1

    def test_reissue_churn_fires_at_threshold(self) -> None:
        evs = [_de("R1", reissue=True, day=d) for d in range(1, 6)]   # 5 reissues
        assert "reissue_churn" in {f.detector for f in run_dossier_audit(evs).flags}

    def test_few_reissues_not_churn(self) -> None:
        evs = [_de("R1", reissue=True, day=d) for d in range(1, 4)]   # only 3
        assert "reissue_churn" not in {f.detector for f in run_dossier_audit(evs).flags}

    def test_fee_waiver_is_categorical(self) -> None:
        rep = run_dossier_audit([_de("W1", desc="-> First time reissue charges waived")])
        assert "fee_waiver" in {f.detector for f in rep.flags}
        assert rep.waivers_seen == 1

    def test_pnr_summary_populated(self) -> None:
        rep = run_dossier_audit([_de("P1", reissue=True), _de("P1", desc="charges waived")])
        s = next(s for s in rep.pnr_summary if s.pnr == "P1")
        assert s.events == 2 and s.reissues == 1 and s.fee_waivers == 1 and s.distinct_agents == 1


# --------------------------------------------------------------------------- downloader (mocked)
class _Resp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status
        self.url = "https://usba.ttinteractive.com/newui/aerien/commun/search_event.asp"


class _ReqSession:
    def __init__(self, html):
        self._html = html
        self.headers: dict = {}
        self.get_calls = 0

    def get(self, url, params=None, timeout=None):
        self.get_calls += 1
        return _Resp(self._html)


class _FakeZenith:
    def __init__(self, html):
        self.session = _ReqSession(html)


def _patch_lookup(monkeypatch):
    import src.zenith_pnr_history_downloader as dl
    monkeypatch.setattr(
        dl, "lookup_pnr",
        lambda session, pnr: type("D", (), {"dossier_id": "DOS" + pnr})())
    return dl


class TestDownloader:
    def test_scrape_then_cache(self, monkeypatch, tmp_path) -> None:
        dl = _patch_lookup(monkeypatch)
        html = _changes_html(_row(
            "14/06/2026 07:50", _AGENT,
            "Comment: PAX CONTACT-1 -&gt; PAX CONTACT-2<br> BKASH PAYMENT//Transaction ID-TXN1//<br>"))
        cache = ZenithPNRHistoryCache(tmp_path / "h.sqlite")
        evs, stats = dl.scrape_dossier_events(_FakeZenith(html), ["09AHEA"], cache=cache, delay_s=0)
        assert stats.resolved == 1 and stats.scraped == 1 and stats.failed == 0
        assert evs and evs[0].payment_txn_id == "TXN1"
        # second run: served from the fresh cache, no live fetch
        evs2, stats2 = dl.scrape_dossier_events(_FakeZenith(html), ["09AHEA"], cache=cache, delay_s=0)
        assert stats2.from_cache == 1 and stats2.scraped == 0
        assert evs2 and evs2[0].payment_txn_id == "TXN1"

    def test_budget_aborts(self, monkeypatch, tmp_path) -> None:
        dl = _patch_lookup(monkeypatch)
        cache = ZenithPNRHistoryCache(tmp_path / "h2.sqlite")
        # budget of 1 covers only the lookup; the fetch can't run -> aborted, nothing scraped
        _, stats = dl.scrape_dossier_events(
            _FakeZenith(_changes_html()), ["AAA"], cache=cache, delay_s=0, max_requests=1)
        assert stats.aborted is True and stats.scraped == 0

    def test_stop_flag(self, monkeypatch, tmp_path) -> None:
        dl = _patch_lookup(monkeypatch)
        cache = ZenithPNRHistoryCache(tmp_path / "h3.sqlite")
        _, stats = dl.scrape_dossier_events(
            _FakeZenith(_changes_html()), ["AAA", "BBB"], cache=cache, delay_s=0,
            stop_flag=lambda: True)
        assert stats.aborted is True and stats.requested == 0


# --------------------------------------------------------------------------- excel header row
class TestExcelHeaderRow:
    """The Reissues 'All Reissues (detail)' sheet puts a title + blank above the header."""

    def _make(self, tmp_path, *, title: bool):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        if title:
            ws.append(["ALL REISSUES — MASTER DETAIL"])
            ws.append([None, None, None, None])
        ws.append(["Date", "Counter", "Original Ticket #", "PNR"])
        ws.append(["2026-01-01 00:00:00", "BO-1", "7792000000001", "14631077"])
        ws.append(["2026-01-02 00:00:00", "BO-2", "7792000000002", "14852997"])
        p = tmp_path / "r.xlsx"
        wb.save(p)
        return p

    def test_finds_pnr_under_title_rows(self, tmp_path) -> None:
        from src.excel_io import list_columns, read_pnr_codes_from_excel
        p = self._make(tmp_path, title=True)
        assert "PNR" in list_columns(p, "Sheet")           # combo now shows real headers
        assert read_pnr_codes_from_excel(p, column_name="PNR") == ["14631077", "14852997"]

    def test_normal_header_row0_still_works(self, tmp_path) -> None:
        from src.excel_io import read_pnr_codes_from_excel
        p = self._make(tmp_path, title=False)
        assert read_pnr_codes_from_excel(p, column_name="PNR") == ["14631077", "14852997"]

    def test_wrong_column_does_not_return_dates(self, tmp_path) -> None:
        # The old bug: column not found -> fell back to col 0 (Date). Now col 0 of the real
        # header is "Date" only if explicitly chosen; an unknown name still falls to col 0,
        # but col 0 is now the real header's first column, not a title/date column.
        from src.excel_io import read_pnr_codes_from_excel
        p = self._make(tmp_path, title=True)
        got = read_pnr_codes_from_excel(p, column_name="PNR")
        assert all(":" not in c for c in got)              # never date-like

    def test_numeric_pnr_trailing_zero_stripped(self, tmp_path) -> None:
        import openpyxl
        from src.excel_io import read_pnr_codes_from_excel
        wb = openpyxl.Workbook(); ws = wb.active
        ws.append(["PNR"]); ws.append([14631077.0])
        p = tmp_path / "n.xlsx"; wb.save(p)
        assert read_pnr_codes_from_excel(p, column_name="PNR") == ["14631077"]
