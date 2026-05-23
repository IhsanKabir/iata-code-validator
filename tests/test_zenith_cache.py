"""Tests for src.zenith_cache."""

from __future__ import annotations

from src.zenith_cache import ZenithCache
from src.zenith_client import (
    STATUS_ERROR,
    STATUS_NOT_FOUND,
    STATUS_OK,
    CustomerRecord,
    LookupResult,
)


def _ok_result(cid: str, **kw) -> LookupResult:
    return LookupResult(
        customer_id=cid,
        status=STATUS_OK,
        record=CustomerRecord(customer_id=cid, **kw),
        checked_at="2026-05-23 12:00:00",
    )


def _not_found(cid: str) -> LookupResult:
    return LookupResult(
        customer_id=cid, status=STATUS_NOT_FOUND,
        error="No customer fields in page",
        checked_at="2026-05-23 12:00:00",
    )


def _error(cid: str, msg: str = "boom") -> LookupResult:
    return LookupResult(
        customer_id=cid, status=STATUS_ERROR, error=msg,
        checked_at="2026-05-23 12:00:00",
    )


def test_save_and_get_ok_record(tmp_path):
    cache = ZenithCache(tmp_path / "z.db")
    r = _ok_result(
        "10000001", first_name="Tester", last_name="Sample",
        email="test.user@example.com", country="Bangladesh",
    )
    cache.save_result(r)
    got = cache.get_result("10000001")
    assert got is not None
    assert got.status == STATUS_OK
    assert got.record.first_name == "Tester"
    assert got.record.email == "test.user@example.com"


def test_save_result_is_idempotent_upsert(tmp_path):
    cache = ZenithCache(tmp_path / "z.db")
    cache.save_result(_ok_result("123", first_name="Old"))
    cache.save_result(_ok_result("123", first_name="New"))
    got = cache.get_result("123")
    assert got.record.first_name == "New"


def test_cached_ids_includes_all_statuses_by_default(tmp_path):
    cache = ZenithCache(tmp_path / "z.db")
    cache.save_result(_ok_result("1"))
    cache.save_result(_not_found("2"))
    cache.save_result(_error("3"))
    assert cache.cached_ids() == {"1", "2", "3"}


def test_cached_ids_only_ok_excludes_failures(tmp_path):
    cache = ZenithCache(tmp_path / "z.db")
    cache.save_result(_ok_result("1"))
    cache.save_result(_not_found("2"))
    cache.save_result(_error("3"))
    assert cache.cached_ids(only_ok=True) == {"1"}


def test_counts_by_status(tmp_path):
    cache = ZenithCache(tmp_path / "z.db")
    cache.save_result(_ok_result("1"))
    cache.save_result(_ok_result("2"))
    cache.save_result(_not_found("3"))
    cache.save_result(_error("4"))
    counts = cache.counts_by_status()
    assert counts[STATUS_OK] == 2
    assert counts[STATUS_NOT_FOUND] == 1
    assert counts[STATUS_ERROR] == 1


def test_clear_errors_only_drops_error_rows(tmp_path):
    cache = ZenithCache(tmp_path / "z.db")
    cache.save_result(_ok_result("1"))
    cache.save_result(_not_found("2"))
    cache.save_result(_error("3"))
    cache.save_result(_error("4"))
    dropped = cache.clear_errors()
    assert dropped == 2
    assert cache.cached_ids() == {"1", "2"}


def test_iter_all_ordered_by_id(tmp_path):
    cache = ZenithCache(tmp_path / "z.db")
    cache.save_result(_ok_result("10000003"))
    cache.save_result(_ok_result("10000001"))
    cache.save_result(_ok_result("10000002"))
    ids = [r.customer_id for r in cache.iter_all()]
    assert ids == ["10000001", "10000002", "10000003"]


def test_reset_clears_everything(tmp_path):
    cache = ZenithCache(tmp_path / "z.db")
    cache.save_result(_ok_result("1"))
    cache.set_meta("last_run", "2026-05-23")
    cache.reset()
    assert cache.cached_ids() == set()
    assert cache.get_meta("last_run") is None


def test_meta_roundtrip(tmp_path):
    cache = ZenithCache(tmp_path / "z.db")
    cache.set_meta("last_run", "2026-05-23 12:00")
    assert cache.get_meta("last_run") == "2026-05-23 12:00"
    cache.set_meta("last_run", "2026-05-23 13:00")
    assert cache.get_meta("last_run") == "2026-05-23 13:00"
