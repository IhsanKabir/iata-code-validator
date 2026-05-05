"""Tests for parser.parse_result — covers valid/invalid/edge cases."""

import pytest

from src.parser import parse_result


VALID_TEXT = """\
ChchACode Evaluation
Validate IATA Agency Codes and Travel Agent ID Cards
Validate
32302491 is a Valid IATA Numeric Code
AGENCY DETAILS
Trading Name    TRAVEL POINT PTE. LTD.    Country  SINGAPORE
This is an IATA Accredited Agent. For more information about this Agent, consider the professional edition of CheckACode or visit www.iata.org/globaldata for more options.
"""

VALID_TEXT_LINES_LAYOUT = """\
32302491 is a Valid IATA Numeric Code
AGENCY DETAILS
Trading Name
TRAVEL POINT PTE. LTD.
Country
SINGAPORE
This is an IATA Accredited Agent.
"""

INVALID_TEXT = """\
00000000 is not a valid IATA Numeric Code
"""

NON_ACCREDITED_TEXT = """\
12345678 is a Valid IATA Numeric Code
AGENCY DETAILS
Trading Name    SOME AGENCY    Country  USA
This is not an IATA Accredited Agent.
"""


def test_valid_inline_layout():
    r = parse_result("32302491", VALID_TEXT)
    assert r.status == "VALID"
    assert r.trading_name == "TRAVEL POINT PTE. LTD."
    assert r.country == "SINGAPORE"
    assert r.accredited == "Y"
    assert r.notes == ""


def test_valid_lines_layout():
    r = parse_result("32302491", VALID_TEXT_LINES_LAYOUT)
    assert r.status == "VALID"
    assert r.trading_name == "TRAVEL POINT PTE. LTD."
    assert r.country == "SINGAPORE"
    assert r.accredited == "Y"


def test_invalid():
    r = parse_result("00000000", INVALID_TEXT)
    assert r.status == "INVALID"
    assert r.trading_name == ""
    assert r.country == ""
    assert r.accredited == ""


def test_non_accredited():
    r = parse_result("12345678", NON_ACCREDITED_TEXT)
    assert r.status == "VALID"
    assert r.accredited == "N"


def test_empty_text():
    r = parse_result("99999999", "")
    assert r.status == "ERROR"
    assert "empty" in r.notes


def test_garbled_text():
    r = parse_result("99999999", "completely unrelated page content")
    assert r.status == "ERROR"
