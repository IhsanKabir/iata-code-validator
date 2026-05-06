"""Tests for bd_matcher matching priority + tagging + selectable fields."""

import pytest

from src.bd_agency_client import Agency
from src.bd_matcher import (
    ALL_FIELDS,
    AgencyIndex,
    DEFAULT_FIELDS,
    FIELD_ADDRESS,
    FIELD_LICENSE,
    FIELD_NAME,
    FUZZY_THRESHOLD,
)


def _agency(
    name: str,
    license_no: str,
    raw_id: int = 1,
    address: str = "",
) -> Agency:
    return Agency(
        agency_name=name,
        license_no=license_no,
        email="",
        mobile="",
        website="",
        address=address,
        license_expired_date="",
        status="ACTIVE",
        raw_id=raw_id,
    )


@pytest.fixture
def index() -> AgencyIndex:
    return AgencyIndex([
        _agency(
            "ZEPHYR TOURS & TRAVELS", "0017198", 1,
            address="380/3, East Rampura, Dhaka-1219",
        ),
        _agency(
            "ForR Tours And Travels", "0017175", 2,
            address="House No-14/A, Banani, Dhaka-1213",
        ),
        _agency(
            "DUA AIR TRAVELS & TOURS", "0017150", 3,
            address="Plot-02, Merul Badda, Dhaka-1212",
        ),
        _agency(
            "Vibe international travels", "0017165", 4,
            address="292, Inner Circular Road, Motijheel, Dhaka-1000",
        ),
        _agency(
            "AMTC HOLIDAYS AND AVIATION", "0017164", 5,
            address="KA-74, Kuril, Vatara, Dhaka-1229",
        ),
        _agency("Zephyr Holdings", "0099999", 6),  # contains-multi case
    ])


def test_exact_match_on_name(index):
    r = index.lookup("ZEPHYR TOURS & TRAVELS")
    assert r.match_method == "EXACT"
    assert r.match_score == 100
    assert r.matched_field == FIELD_NAME
    assert r.agency.license_no == "0017198"
    assert r.other_matches == 0


def test_exact_match_case_insensitive(index):
    r = index.lookup("zephyr tours & travels")
    assert r.match_method == "EXACT"
    assert r.matched_field == FIELD_NAME


def test_exact_match_on_license_number(index):
    r = index.lookup("0017175")
    assert r.match_method == "EXACT"
    assert r.matched_field == FIELD_LICENSE
    assert r.agency.agency_name == "ForR Tours And Travels"


def test_contains_single_match(index):
    r = index.lookup("AMTC")
    assert r.match_method == "CONTAINS"
    assert r.matched_field == FIELD_NAME
    assert r.agency.raw_id == 5


def test_contains_multiple_match(index):
    r = index.lookup("Zephyr")
    assert r.match_method == "MULTIPLE_CONTAINS"
    assert r.other_matches == 1
    assert r.matched_field == FIELD_NAME


def test_fuzzy_match_typo(index):
    r = index.lookup("ZEPHIR TOURS & TRAVELS")
    assert r.match_method == "FUZZY"
    assert r.match_score >= FUZZY_THRESHOLD
    assert r.matched_field == FIELD_NAME


def test_no_match(index):
    r = index.lookup("totally unrelated string xyz")
    assert r.match_method == "NO_MATCH"
    assert r.matched_field == ""
    assert r.agency is None


def test_default_fields_skip_address(index):
    """With DEFAULT_FIELDS (Name+License), address is NOT searched."""
    # 'Rampura' is in ZEPHYR's address but not its name/license
    r = index.lookup("Rampura")
    assert r.match_method == "NO_MATCH"


def test_address_search_when_enabled(index):
    r = index.lookup("Rampura", fields=ALL_FIELDS)
    assert r.match_method == "CONTAINS"
    assert r.matched_field == FIELD_ADDRESS
    assert r.agency.raw_id == 1


def test_address_only_field(index):
    """Searching only Address never matches by name/license."""
    r = index.lookup("ZEPHYR TOURS & TRAVELS", fields=(FIELD_ADDRESS,))
    assert r.match_method == "NO_MATCH"


def test_address_fuzzy(index):
    """Fuzzy match also runs against address when address is enabled."""
    # Drop a single character to force FUZZY (not CONTAINS).
    # "Motijhel" is a 1-char delete from "Motijheel" in Vibe's address;
    # rapidfuzz partial_ratio scores ≥ 85.
    r = index.lookup("Motijhel", fields=(FIELD_NAME, FIELD_ADDRESS))
    assert r.match_method == "FUZZY"
    assert r.match_score >= FUZZY_THRESHOLD
    assert r.matched_field == FIELD_ADDRESS


def test_priority_exact_beats_contains(index):
    idx = AgencyIndex([
        _agency("AB", "001", 1),
        _agency("AB Travels", "002", 2),
    ])
    r = idx.lookup("AB")
    assert r.match_method == "EXACT"
    assert r.agency.raw_id == 1


def test_field_priority_name_before_license(index):
    """When the same query EXACT-matches multiple fields, name wins first
    because it's earlier in the DEFAULT_FIELDS tuple."""
    idx = AgencyIndex([
        _agency("12345", "99999", raw_id=1),  # name happens to be numeric
        _agency("Other Travels", "12345", raw_id=2),
    ])
    r = idx.lookup("12345", fields=(FIELD_NAME, FIELD_LICENSE))
    # Name field is checked first → agency 1 wins
    assert r.matched_field == FIELD_NAME
    assert r.agency.raw_id == 1


def test_empty_input(index):
    r = index.lookup("")
    assert r.match_method == "NO_MATCH"
    assert r.matched_field == ""


def test_searched_input_preserved(index):
    r = index.lookup("  ZEPHYR TOURS & TRAVELS  ")
    assert r.searched_input == "  ZEPHYR TOURS & TRAVELS  "
    assert r.match_method == "EXACT"
