"""Tests for bd_matcher matching priority + tagging."""

import pytest

from src.bd_agency_client import Agency
from src.bd_matcher import AgencyIndex, FUZZY_THRESHOLD


def _agency(name: str, license_no: str, raw_id: int = 1) -> Agency:
    return Agency(
        agency_name=name,
        license_no=license_no,
        email="",
        mobile="",
        website="",
        address="",
        license_expired_date="",
        status="ACTIVE",
        raw_id=raw_id,
    )


@pytest.fixture
def index() -> AgencyIndex:
    return AgencyIndex([
        _agency("ZEPHYR TOURS & TRAVELS", "0017198", 1),
        _agency("ForR Tours And Travels", "0017175", 2),
        _agency("DUA AIR TRAVELS & TOURS", "0017150", 3),
        _agency("Vibe international travels", "0017165", 4),
        _agency("AMTC HOLIDAYS AND AVIATION", "0017164", 5),
        _agency("Zephyr Holdings", "0099999", 6),  # contains-multi case
    ])


def test_exact_match_on_name(index):
    r = index.lookup("ZEPHYR TOURS & TRAVELS")
    assert r.match_method == "EXACT"
    assert r.match_score == 100
    assert r.agency.license_no == "0017198"
    assert r.other_matches == 0


def test_exact_match_case_insensitive(index):
    r = index.lookup("zephyr tours & travels")
    assert r.match_method == "EXACT"
    assert r.agency.raw_id == 1


def test_exact_match_on_license_number(index):
    r = index.lookup("0017175")
    assert r.match_method == "EXACT"
    assert r.agency.agency_name == "ForR Tours And Travels"


def test_contains_single_match(index):
    r = index.lookup("AMTC")
    assert r.match_method == "CONTAINS"
    assert r.match_score == 100
    assert r.agency.raw_id == 5


def test_contains_multiple_match(index):
    # "Zephyr" appears in both ZEPHYR TOURS & TRAVELS and Zephyr Holdings
    r = index.lookup("Zephyr")
    assert r.match_method == "MULTIPLE_CONTAINS"
    assert r.other_matches == 1


def test_fuzzy_match_typo(index):
    # "ZEPHIR" is a one-letter typo of "ZEPHYR"
    r = index.lookup("ZEPHIR TOURS & TRAVELS")
    assert r.match_method == "FUZZY"
    assert r.match_score >= FUZZY_THRESHOLD


def test_no_match(index):
    r = index.lookup("totally unrelated string xyz")
    assert r.match_method == "NO_MATCH"
    assert r.agency is None
    assert r.match_score == 0


def test_empty_input(index):
    r = index.lookup("")
    assert r.match_method == "NO_MATCH"
    assert r.agency is None


def test_searched_input_preserved(index):
    r = index.lookup("  ZEPHYR TOURS & TRAVELS  ")  # padded
    assert r.searched_input == "  ZEPHYR TOURS & TRAVELS  "
    assert r.match_method == "EXACT"  # exact still works after trim


def test_priority_exact_beats_contains(index):
    # If we add an entry whose name contains the input value as a substring,
    # but another entry exactly matches, exact should win.
    idx = AgencyIndex([
        _agency("AB", "001", 1),
        _agency("AB Travels", "002", 2),
    ])
    r = idx.lookup("AB")
    assert r.match_method == "EXACT"
    assert r.agency.raw_id == 1
