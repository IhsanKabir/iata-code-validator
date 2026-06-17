"""Tests for src/zenith_pnr_history_cache.py — the raw gzipped-HTML scrape cache.

The cache stores raw HTML bytes per (dossier, tab) so parser fixes re-run offline.
These tests verify the round-trip, atomic per-dossier replace, TTL freshness, and
retention purge — all without touching Zenith.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from src.zenith_pnr_history_cache import RawTab, ZenithPNRHistoryCache


def _cache(tmp_path) -> ZenithPNRHistoryCache:
    return ZenithPNRHistoryCache(tmp_path / "hist.sqlite")


def test_round_trip_preserves_html_and_metadata(tmp_path) -> None:
    c = _cache(tmp_path)
    html = "<table><tr><td>15/06/2026</td><td>taposh2589</td></tr></table>"
    c.put_bundle("12345", {"changes": (html, 200), "tickets": ("<table></table>", 200)})
    bundle = c.get_bundle("12345")
    assert bundle is not None
    assert set(bundle) == {"changes", "tickets"}
    assert bundle["changes"].html == html              # decompresses to the exact bytes
    assert bundle["changes"].http_status == 200
    assert bundle["changes"].byte_size == len(html.encode("utf-8"))
    assert isinstance(bundle["changes"].fetched_at, datetime)


def test_missing_dossier_returns_none(tmp_path) -> None:
    c = _cache(tmp_path)
    assert c.get_bundle("nope") is None
    assert c.get_bundle("") is None


def test_put_bundle_replaces_atomically(tmp_path) -> None:
    c = _cache(tmp_path)
    c.put_bundle("D1", {"changes": ("old", 200), "tickets": ("t", 200)})
    # Re-scrape with a DIFFERENT tab set — old tabs must be gone, not merged.
    c.put_bundle("D1", {"changes": ("new", 200)})
    bundle = c.get_bundle("D1")
    assert set(bundle) == {"changes"}
    assert bundle["changes"].html == "new"


def test_empty_inputs_are_noops(tmp_path) -> None:
    c = _cache(tmp_path)
    c.put_bundle("", {"changes": ("x", 200)})
    c.put_bundle("D1", {})
    assert c.count_dossiers() == 0


def test_is_fresh_window(tmp_path) -> None:
    c = _cache(tmp_path)
    now = datetime(2026, 6, 17, 12, 0, 0)
    c.put_bundle("D1", {"changes": ("x", 200)}, now=now - timedelta(days=1))
    assert c.is_fresh("D1", stale_after_days=3, now=now) is True
    assert c.is_fresh("D1", stale_after_days=0.5, now=now) is False   # older than window
    assert c.is_fresh("absent", stale_after_days=3, now=now) is False


def test_is_fresh_false_if_any_tab_stale(tmp_path) -> None:
    c = _cache(tmp_path)
    now = datetime(2026, 6, 17, 12, 0, 0)
    # write one fresh tab, then add a stale one by re-putting both with mixed stamps
    c.put_bundle("D1", {"fresh": ("a", 200)}, now=now)
    # Manually age one tab via a second dossier-scoped write isn't possible (atomic
    # replace), so simulate by putting both at an old time then one fresh is the real
    # contract: a bundle shares one fetched_at. Verify the shared-timestamp behaviour.
    c.put_bundle("D1", {"a": ("x", 200), "b": ("y", 200)}, now=now - timedelta(days=5))
    assert c.is_fresh("D1", stale_after_days=3, now=now) is False


def test_purge_older_than(tmp_path) -> None:
    c = _cache(tmp_path)
    now = datetime(2026, 6, 17, 12, 0, 0)
    c.put_bundle("OLD", {"changes": ("x", 200)}, now=now - timedelta(days=40))
    c.put_bundle("NEW", {"changes": ("y", 200)}, now=now - timedelta(days=1))
    removed = c.purge_older_than(30, now=now)
    assert removed == 1
    assert c.get_bundle("OLD") is None
    assert c.get_bundle("NEW") is not None


def test_counts(tmp_path) -> None:
    c = _cache(tmp_path)
    c.put_bundle("D1", {"changes": ("x", 200), "tickets": ("y", 200)})
    c.put_bundle("D2", {"changes": ("z", 200)})
    assert c.count_dossiers() == 2
    assert c.count_tabs() == 3


def test_clear(tmp_path) -> None:
    c = _cache(tmp_path)
    c.put_bundle("D1", {"changes": ("x", 200)})
    c.clear()
    assert c.count_dossiers() == 0


def test_rawtab_dataclass_shape(tmp_path) -> None:
    c = _cache(tmp_path)
    c.put_bundle("D1", {"changes": ("<html>x</html>", 200)})
    tab = c.get_bundle("D1")["changes"]
    assert isinstance(tab, RawTab)
    assert tab.dossier_id == "D1" and tab.tab == "changes"
    assert tab.scrape_version >= 1
