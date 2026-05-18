"""Tests for the OEP preset store — pure JSON round-trip, no network."""

import json

import pytest

from src.oep_presets import OEPPreset, PresetStore


@pytest.fixture
def store(tmp_path):
    return PresetStore(tmp_path / "presets.json")


def test_empty_store_lists_nothing(store):
    assert store.list_names() == []
    assert store.list() == []
    assert store.get("anything") is None


def test_save_and_load_roundtrip(store):
    p = OEPPreset(
        name="Saudi Q1 2026",
        mode="timeseries",
        date_from="2026-01-01",
        date_to="2026-03-31",
        gender_id="1",
        country_ids=["154"],
        country_labels=["Saudi Arabia"],
    )
    store.save(p)
    assert store.list_names() == ["Saudi Q1 2026"]
    got = store.get("Saudi Q1 2026")
    assert got == p


def test_save_replaces_existing_by_name(store):
    store.save(OEPPreset(
        name="Default", mode="country",
        date_from="2025-01-01", date_to="2025-06-30",
    ))
    store.save(OEPPreset(
        name="Default", mode="timeseries",
        date_from="2024-01-01", date_to="2024-12-31",
    ))
    presets = store.list()
    assert len(presets) == 1
    assert presets[0].mode == "timeseries"
    assert presets[0].date_from == "2024-01-01"


def test_save_rejects_blank_name(store):
    with pytest.raises(ValueError):
        store.save(OEPPreset(name="  ", mode="country",
                             date_from="2025-01-01", date_to="2025-01-31"))


def test_delete_returns_true_on_hit_and_false_on_miss(store):
    store.save(OEPPreset(name="A", mode="country",
                         date_from="2025-01-01", date_to="2025-01-31"))
    assert store.delete("A") is True
    assert store.list_names() == []
    assert store.delete("ghost") is False


def test_list_returns_sorted_alphabetically(store):
    for name in ("Zebra", "Alpha", "mike"):
        store.save(OEPPreset(
            name=name, mode="country",
            date_from="2025-01-01", date_to="2025-01-31",
        ))
    assert store.list_names() == ["Alpha", "mike", "Zebra"]


def test_corrupt_json_does_not_crash(store, tmp_path):
    (tmp_path / "presets.json").write_text("{not json", encoding="utf-8")
    # Should silently treat as empty, not raise
    assert store.list() == []
    # And saving a fresh preset should work after corruption
    store.save(OEPPreset(name="X", mode="country",
                         date_from="2025-01-01", date_to="2025-01-31"))
    assert store.list_names() == ["X"]


def test_non_list_root_is_ignored(store, tmp_path):
    (tmp_path / "presets.json").write_text(
        json.dumps({"not": "a list"}), encoding="utf-8",
    )
    assert store.list() == []


def test_malformed_entries_are_skipped_silently(store, tmp_path):
    (tmp_path / "presets.json").write_text(
        json.dumps([
            "string-instead-of-dict",
            {"name": "Good", "mode": "country",
             "date_from": "2025-01-01", "date_to": "2025-01-31"},
            None,
            42,
        ]),
        encoding="utf-8",
    )
    assert store.list_names() == ["Good"]
