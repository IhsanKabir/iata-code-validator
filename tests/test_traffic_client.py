"""Tests for the Traffic data layer + the Malaysia arrivals extractor.

The SAMPLE mirrors the REAL data.gov.my arrivals payload shape (verified live):
{"date","country","arrivals","arrivals_male","arrivals_female"}. No network.
"""

from __future__ import annotations

import pytest

from src import traffic_client as tc
from src.traffic_sources import SOURCES
from src.traffic_sources import malaysia_arrivals as my


SAMPLE = [
    {"date": "2020-01-01", "country": "ALL",
     "arrivals": 2923053, "arrivals_male": 1598823, "arrivals_female": 1324230},
    {"date": "2020-02-01", "country": "Singapore",
     "arrivals": 100, "arrivals_male": 60, "arrivals_female": 40},
    {"date": "2020-03-01", "country": "Singapore",
     "arrivals": 50, "arrivals_male": 30, "arrivals_female": 20},
]


# ---- parsing ----

def test_parse_arrivals_shape():
    rows = my.parse_arrivals(SAMPLE)
    assert len(rows) == 3
    r0 = rows[0]
    assert r0.source == "malaysia_arrivals"
    assert r0.period == "2020-01" and r0.period_granularity == "month"
    assert r0.metric == "arrivals" and r0.unit == "passengers"
    assert r0.value == 2923053.0
    assert r0.country == "All countries"      # "ALL" normalized
    assert r0.direction == "arrival" and r0.flight_type == "international"
    assert rows[1].country == "Singapore"


def test_parse_skips_blank_date():
    assert my.parse_arrivals([{"country": "X", "arrivals": 5}]) == []


def test_parse_handles_bad_numbers():
    rows = my.parse_arrivals([{"date": "2021-05-01", "country": "X", "arrivals": None}])
    assert rows[0].value == 0.0


# ---- aggregation helpers ----

def test_aggregate_by_country():
    totals = tc.aggregate_by_country(my.parse_arrivals(SAMPLE))
    by = {t.country: t.value for t in totals}
    assert by["Singapore"] == 150.0           # 100 + 50
    assert by["All countries"] == 2923053.0
    assert totals[0].country == "All countries"  # sorted desc by value


def test_aggregate_by_period_sorted():
    per = tc.aggregate_by_period(my.parse_arrivals(SAMPLE))
    assert [p.period for p in per] == ["2020-01", "2020-02", "2020-03"]
    assert {p.period: p.value for p in per}["2020-02"] == 100.0


# ---- registry / protocol contract ----

def test_registry_contract():
    assert {"malaysia_arrivals", "bts_t100"} <= set(SOURCES)
    for sid, s in SOURCES.items():
        for attr in ("id", "label", "granularity", "needs_credentials", "needs_file"):
            assert hasattr(s, attr), f"{sid} missing {attr}"
        assert callable(s.fetch) and callable(s.list_filter_options)
    assert SOURCES["malaysia_arrivals"].granularity == "country"
    assert SOURCES["bts_t100"].granularity == "route" and SOURCES["bts_t100"].needs_file is True


# ---- fetch (mocked HTTP) ----

class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def test_fetch_mocked(monkeypatch):
    monkeypatch.setattr(my, "http_get", lambda *a, **k: _FakeResp(SAMPLE))
    rows = my.SOURCE.fetch({})
    assert len(rows) == 3
    assert {r.country for r in rows} == {"All countries", "Singapore"}


def test_fetch_wrapped_data_key(monkeypatch):
    monkeypatch.setattr(my, "http_get", lambda *a, **k: _FakeResp({"data": SAMPLE}))
    assert len(my.SOURCE.fetch({})) == 3


def test_fetch_date_filter(monkeypatch):
    monkeypatch.setattr(my, "http_get", lambda *a, **k: _FakeResp(SAMPLE))
    rows = my.SOURCE.fetch({"date_from": "2020-02", "date_to": "2020-02"})
    assert {r.period for r in rows} == {"2020-02"}


def test_fetch_bad_payload(monkeypatch):
    monkeypatch.setattr(my, "http_get", lambda *a, **k: _FakeResp("not a list"))
    with pytest.raises(tc.TrafficError):
        my.SOURCE.fetch({})


# ---- BTS T-100 (file-based, route-level) ----

from src.traffic_sources import bts_t100 as bts

_T100 = [
    {"YEAR": "2024", "MONTH": "3", "ORIGIN": "JFK", "DEST": "LHR",
     "UNIQUE_CARRIER": "BA", "PASSENGERS": "30000", "SEATS": "35000"},
    {"YEAR": "2024", "MONTH": "3", "ORIGIN": "jfk", "DEST": "lhr",
     "CARRIER": "AA", "PASSENGERS": "10000", "SEATS": "12000"},
    {"YEAR": "2024", "MONTH": "4", "ORIGIN": "DAC", "DEST": "DXB",
     "CARRIER": "BS", "PASSENGERS": "5000", "SEATS": "6000"},
]


def test_bts_parse_route_rows():
    rows = bts.parse_t100_rows(_T100)
    # 3 segments x (passengers + seats) = 6 rows
    assert len(rows) == 6
    pax = [r for r in rows if r.metric == "passengers"]
    assert {(r.origin, r.destination) for r in pax} == {("JFK", "LHR"), ("DAC", "DXB")}
    jfk = next(r for r in pax if r.carrier == "BA")
    assert jfk.period == "2024-03" and jfk.value == 30000.0 and jfk.unit == "passengers"


def test_bts_aggregate_by_route():
    rt = tc.aggregate_by_route(bts.parse_t100_rows(_T100))
    pax = {(r.origin, r.destination): r.value for r in rt if r.metric == "passengers"}
    assert pax[("JFK", "LHR")] == 40000.0   # BA 30000 + AA 10000 (case-normalized)
    assert pax[("DAC", "DXB")] == 5000.0


def test_bts_skips_rows_without_route():
    assert bts.parse_t100_rows([{"YEAR": "2024", "MONTH": "1", "PASSENGERS": "9"}]) == []


def test_bts_missing_file_errors():
    with pytest.raises(tc.TrafficError):
        bts.SOURCE.fetch({"csv_path": ""})


def test_bts_reads_csv(tmp_path):
    import csv as _csv
    p = tmp_path / "t100.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=["YEAR", "MONTH", "ORIGIN", "DEST", "CARRIER", "PASSENGERS", "SEATS"])
        w.writeheader()
        w.writerow({"YEAR": "2024", "MONTH": "3", "ORIGIN": "DAC", "DEST": "DXB",
                    "CARRIER": "BS", "PASSENGERS": "5000", "SEATS": "6000"})
    rows = bts.SOURCE.fetch({"csv_path": str(p)})
    assert any(r.origin == "DAC" and r.destination == "DXB" and r.value == 5000.0 for r in rows)
