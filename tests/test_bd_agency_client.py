"""Tests for bd_agency_client field parsing.

Doesn't hit the network — exercises the HTML-blob parsing of records like
the API actually returns.
"""

from datetime import date, timedelta

from src.bd_agency_client import (
    _classify_status,
    _split_contact,
    _split_name_license,
    parse_agency_record,
)


def test_split_name_license_two_lines():
    name, lic = _split_name_license("ZEPHYR TOURS & TRAVELS<br/>0017198")
    assert name == "ZEPHYR TOURS & TRAVELS"
    assert lic == "0017198"


def test_split_name_license_with_self_closing_br():
    name, lic = _split_name_license("AB Travels<br />0099999")
    assert name == "AB Travels"
    assert lic == "0099999"


def test_split_name_license_only_name():
    name, lic = _split_name_license("Solo Travels")
    assert name == "Solo Travels"
    assert lic == ""


def test_split_name_license_empty():
    name, lic = _split_name_license("")
    assert name == ""
    assert lic == ""


def test_split_contact_email_mobile():
    email, mobile, web = _split_contact(
        "zephyrtourism@gmail.com<br/>01912100770<br/>"
    )
    assert email == "zephyrtourism@gmail.com"
    assert mobile == "01912100770"
    assert web == ""


def test_split_contact_with_website():
    email, mobile, web = _split_contact(
        "info@example.com<br/>01711314927<br/>www.example.com.bd"
    )
    assert email == "info@example.com"
    assert mobile == "01711314927"
    assert web == "www.example.com.bd"


def test_split_contact_handles_88_prefix():
    _, mobile, _ = _split_contact("01987654321")
    assert mobile == "01987654321"


def test_classify_status_active_future_date():
    future = (date.today() + timedelta(days=180)).isoformat()
    assert _classify_status(future) == "ACTIVE"


def test_classify_status_expired_pending():
    past = (date.today() - timedelta(days=10)).isoformat()
    assert _classify_status(past) == "EXPIRED-PENDING"


def test_classify_status_today_is_active():
    today = date.today().isoformat()
    assert _classify_status(today) == "ACTIVE"


def test_classify_status_empty_falls_back_to_active():
    assert _classify_status("") == "ACTIVE"


def test_classify_status_unparseable_falls_back_to_active():
    assert _classify_status("not-a-date") == "ACTIVE"


def test_parse_agency_record_full():
    raw = {
        "agency_name_license": "ZEPHYR TOURS & TRAVELS<br/>0017198",
        "agency_email_number_website": "zephyrtourism@gmail.com<br/>01912100770<br/>",
        "business_address_en": "380/3, East Rampura, Dhaka-1219",
        "license_expired_date": (date.today() + timedelta(days=365)).isoformat(),
        "is_approved": 1,
        "id": 16318,
    }
    a = parse_agency_record(raw)
    assert a.agency_name == "ZEPHYR TOURS & TRAVELS"
    assert a.license_no == "0017198"
    assert a.email == "zephyrtourism@gmail.com"
    assert a.mobile == "01912100770"
    assert a.website == ""
    assert "Dhaka-1219" in a.address
    assert a.status == "ACTIVE"
    assert a.raw_id == 16318


def test_parse_agency_record_multiline_address():
    raw = {
        "agency_name_license": "ForR Tours And Travels<br/>0017175",
        "agency_email_number_website": "forrtoursandtravels@gmail.com<br/>01781802110<br/>",
        "business_address_en": (
            "House No-14/A , Flat No-B/2, Road-2/2,<br />\n"
            "Block-L, Banani Chairman Bari,<br />\n"
            "Dhaka-1213"
        ),
        "license_expired_date": "2029-05-04",
        "is_approved": 1,
        "id": 16296,
    }
    a = parse_agency_record(raw)
    assert a.agency_name == "ForR Tours And Travels"
    assert a.license_no == "0017175"
    # Address is collapsed onto one line with comma separators
    assert "House No-14/A" in a.address
    assert "Banani Chairman Bari" in a.address
    assert "Dhaka-1213" in a.address
    # No <br> tags should leak through
    assert "<br" not in a.address
