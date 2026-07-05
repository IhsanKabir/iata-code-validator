"""Tests for the WhatsApp data layer — phone normalization, dedup, row build,
image validation, speed presets. No browser, no network."""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from src.whatsapp_io import (
    SPEED_PRESETS,
    build_whatsapp_rows,
    normalize_phone,
    read_whatsapp_rows,
    validate_image,
)


# --- phone normalization (Bangladesh default +880) -------------------------

@pytest.mark.parametrize("raw,expected", [
    ("01812377362", "8801812377362"),      # BD local, leading 0 -> 880
    ("+8801812377362", "8801812377362"),   # already intl with +
    ("8801812377362", "8801812377362"),    # already intl no +
    ("008801812377362", "8801812377362"),  # 00 intl prefix
    ("018-1237-7362", "8801812377362"),    # separators stripped
    (" 01812377362 ", "8801812377362"),    # whitespace
])
def test_normalize_bd_mobiles(raw, expected):
    assert normalize_phone(raw, default_cc="880") == expected


@pytest.mark.parametrize("raw", ["", "   ", "abc", "12345", "01711"])
def test_normalize_rejects_junk_and_too_short(raw):
    assert normalize_phone(raw, default_cc="880") is None


@pytest.mark.parametrize("raw", [
    "1812377362",          # bare, no leading 0, <11 digits -> AMBIGUOUS, reject
    "4155551234",          # a US 10-digit bare number must NOT become +880...
    "415-555-1234 x99",    # extension letters -> reject, never fabricate a number
    "call me 0181",        # stray text
])
def test_normalize_rejects_ambiguous_and_extensions(raw):
    # These previously risked dialling a real WRONG number (e.g. 880+US number).
    assert normalize_phone(raw, default_cc="880") is None


def test_normalize_sanitizes_country_code():
    # a '+' or spaces in the country-code field must not corrupt the number
    assert normalize_phone("01812377362", default_cc="+880") == "8801812377362"
    assert normalize_phone("01812377362", default_cc="  880 ") == "8801812377362"


@pytest.mark.parametrize("raw,expected", [
    ("+14155552671", "14155552671"),        # US, explicit +
    ("+919876543210", "919876543210"),      # India, explicit +
    ("919876543210", "919876543210"),       # India, bare but carries cc (12 digits)
    ("0044 7911 123456", "447911123456"),   # UK via 00 intl prefix
    ("+8613800138000", "8613800138000"),    # China
])
def test_normalize_is_global(raw, expected):
    # a foreign number that already carries its own country code is NEVER
    # rewritten with default_cc — the blast can reach any country.
    assert normalize_phone(raw, default_cc="880") == expected


# --- reading + dedup + unreachable -----------------------------------------

def _sheet(path: Path, rows: list[list], headers=None) -> Path:
    headers = headers or ["FIRSTNAME", "FFP Level", "PHONE MOBILE"]
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


def test_read_rows_groups_dedups_and_buckets_unreachable(tmp_path):
    p = _sheet(tmp_path / "ffp.xlsx", [
        ["zahid", "Silver", "01812377362"],
        ["rafiq", "Gold", "01713046550"],
        ["dupe", "Silver", "01713046550"],   # same number -> deduped
        ["nomobile", "Silver", ""],           # unreachable
        ["badnum", "Bronze", "12"],           # unreachable
    ])
    res = read_whatsapp_rows(p, phone_column="PHONE MOBILE", default_cc="880")
    assert len(res.rows) == 2                       # two unique numbers
    nums = {r.phone for r in res.rows}
    assert nums == {"8801812377362", "8801713046550"}
    assert len(res.unreachable_rows) == 2
    assert res.warnings                              # surfaced to the GUI
    first = next(r for r in res.rows if r.phone == "8801812377362")
    assert first.fields["FIRSTNAME"] == "zahid"
    assert first.fields["firstname"] == "zahid"     # case alias for templates


def test_missing_phone_column_raises(tmp_path):
    p = _sheet(tmp_path / "ffp.xlsx", [["x", "Gold", "01812377362"]])
    with pytest.raises(ValueError, match="olumn"):
        read_whatsapp_rows(p, phone_column="Nope")


def test_dedup_keeps_first_row_fields(tmp_path):
    p = _sheet(tmp_path / "ffp.xlsx", [
        ["first", "Gold", "01713046550"],
        ["second", "Silver", "01713046550"],
    ])
    res = read_whatsapp_rows(p, phone_column="PHONE MOBILE")
    assert len(res.rows) == 1
    assert res.rows[0].fields["FIRSTNAME"] == "first"   # first wins


# --- template row build -----------------------------------------------------

def test_build_rows_renders_message_and_carries_image(tmp_path):
    p = _sheet(tmp_path / "ffp.xlsx", [["zahid", "Silver", "01812377362"]])
    res = read_whatsapp_rows(p, phone_column="PHONE MOBILE")
    img = tmp_path / "promo.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    built = build_whatsapp_rows(res, "Dear {FIRSTNAME}, your {FFP Level} miles await!",
                                image_path=str(img))
    assert len(built) == 1
    assert built[0].text == "Dear zahid, your Silver miles await!"
    assert built[0].image_path == str(img)
    assert built[0].phone == "8801812377362"


def test_build_rows_reports_unknown_placeholder(tmp_path):
    p = _sheet(tmp_path / "ffp.xlsx", [["z", "Silver", "01812377362"]])
    res = read_whatsapp_rows(p, phone_column="PHONE MOBILE")
    built = build_whatsapp_rows(res, "Hi {Nope}", image_path=None)
    assert "{Nope}" in built[0].text          # left verbatim, never crashes


# --- image validation -------------------------------------------------------

def test_validate_image_accepts_png(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 50)
    ok, msg = validate_image(str(img))
    assert ok and msg == ""


def test_validate_image_rejects_missing_and_wrong_type(tmp_path):
    ok, msg = validate_image(str(tmp_path / "nope.png"))
    assert not ok and "not found" in msg.lower()
    txt = tmp_path / "a.txt"
    txt.write_text("hello")
    ok, msg = validate_image(str(txt))
    assert not ok and "image" in msg.lower()


def test_validate_image_none_is_ok():
    assert validate_image(None) == (True, "")   # text-only run


def test_validate_image_rejects_oversize(tmp_path):
    big = tmp_path / "big.jpg"
    big.write_bytes(b"\xff\xd8\xff" + b"0" * (17 * 1024 * 1024))
    ok, msg = validate_image(str(big))
    assert not ok and "16" in msg


# --- speed presets ----------------------------------------------------------

def test_speed_presets_shape_and_ordering():
    assert set(SPEED_PRESETS) >= {"Safe", "Balanced", "Fast"}
    safe, fast = SPEED_PRESETS["Safe"], SPEED_PRESETS["Fast"]
    assert safe.min_delay_s >= fast.min_delay_s      # safe is slower
    assert safe.daily_cap <= fast.daily_cap
    assert fast.risk_level == "high"
    assert safe.risk_level == "low"
