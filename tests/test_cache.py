"""Tests for the SQLite cache."""

from src.cache import Cache
from src.parser import LookupResult


def _result(iata: str, status: str = "VALID") -> LookupResult:
    return LookupResult(
        iata_number=iata,
        trading_name="TEST AGENCY",
        country="USA",
        accredited="Y",
        status=status,
        checked_at="2026-05-05 10:00:00",
        notes="",
    )


def test_cache_round_trip(tmp_path):
    cache = Cache(tmp_path / "c.sqlite")
    assert cache.get("12345678") is None
    cache.put(_result("12345678"))
    got = cache.get("12345678")
    assert got is not None
    assert got.iata_number == "12345678"
    assert got.trading_name == "TEST AGENCY"
    assert got.status == "VALID"


def test_cache_overwrites_on_put(tmp_path):
    cache = Cache(tmp_path / "c.sqlite")
    cache.put(_result("12345678", status="VALID"))
    cache.put(LookupResult(
        iata_number="12345678",
        trading_name="UPDATED",
        country="UK",
        accredited="N",
        status="VALID",
        checked_at="2026-06-01 10:00:00",
        notes="",
    ))
    got = cache.get("12345678")
    assert got is not None
    assert got.trading_name == "UPDATED"
    assert got.country == "UK"


def test_cache_skips_error_rows(tmp_path):
    cache = Cache(tmp_path / "c.sqlite")
    cache.put(_result("12345678", status="ERROR"))
    assert cache.get("12345678") is None
    assert cache.count() == 0


def test_cache_count(tmp_path):
    cache = Cache(tmp_path / "c.sqlite")
    cache.put(_result("11111111"))
    cache.put(_result("22222222"))
    cache.put(_result("33333333"))
    assert cache.count() == 3
