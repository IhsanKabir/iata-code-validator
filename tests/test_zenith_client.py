"""Tests for src.zenith_client.

Uses a synthetic HTML fragment that mirrors the ASP.NET WebForms field
name structure of a real Customer.aspx response. No real customer data
ever lives in this repo — the fixture is built from completely fake
values inside this file.
"""

from __future__ import annotations

import pytest

from src.zenith_client import (
    CustomerNotFoundError,
    LoginError,
    parse_customer_html,
    parse_state_values,
)


def _txt(field: str, value: str) -> str:
    """Render the WebForms-style text input the parser anchors on."""
    name = (
        "mUsrMain$UsrFinalCustomer$UsrFinalCustomer_MainInformation1$" + field
    )
    return f'<input name="{name}" type="text" value="{value}" />'


def _select(field: str, selected_label: str) -> str:
    """Render a WebForms-style dropdown with one selected option."""
    name = (
        "mUsrMain$UsrFinalCustomer$UsrFinalCustomer_MainInformation1$" + field
    )
    return (
        f'<select name="{name}">'
        f'<option value="">--</option>'
        f'<option selected="selected">{selected_label}</option>'
        f"</select>"
    )


def _build_html(**overrides: str) -> str:
    """Build a synthetic Customer.aspx fragment.

    Defaults give the canonical happy-path values used by most tests; any
    keyword overrides replace the corresponding field.
    """
    fields = {
        "txtFirstName": "Tester",
        "txtLastName": "Sample",
        "txtMiddleName": "",
        "txtDateOfBirth": "01/01/1970",
        "txtEmail": "test.user@example.com",
        "txtHomePhoneNumber": "1234567890",
        "txtHomePhoneNumberInternational": "+8801234567890",
        "txtMobilePhoneNumber": "1234567890",
        "txtMobilePhoneNumberInternational": "+8801234567890",
        "txtOfficePhoneNumber": "",
        "txtAddress": "Test Area",
        "txtCity": "Dhaka",
        "txtPostalCode": "1207",
        "txtCountry": "Bangladesh",
    }
    selects = {
        "ddlTitle": "Mr.",
        "ddlNationality": "Select...",  # placeholder — parser should blank it
        "ddlLanguage": "English",
        "ddlSpokenLanguage": "English",
        "ddlCountry": "Bangladesh",
    }
    for k, v in overrides.items():
        if k in fields:
            fields[k] = v
        elif k in selects:
            selects[k] = v

    parts = ["<html><body>"]
    parts.extend(_txt(k, v) for k, v in fields.items())
    parts.extend(_select(k, v) for k, v in selects.items())
    parts.append(
        '<span id="mUsrMain_UsrFinalCustomer_UsrFinalCustomer_MainInformation1_lblRegistrationDate">'
        "01/01/2025</span>"
    )
    parts.append("</body></html>")
    return "".join(parts)


def test_parse_customer_extracts_main_information():
    record = parse_customer_html(_build_html(), "10000001")
    assert record.customer_id == "10000001"
    assert record.first_name == "Tester"
    assert record.last_name == "Sample"
    assert record.title == "Mr."
    assert record.email == "test.user@example.com"
    assert record.date_of_birth == "01/01/1970"


def test_parse_customer_extracts_phone_numbers():
    record = parse_customer_html(_build_html(), "10000001")
    assert record.home_phone == "1234567890"
    assert record.home_phone_international == "+8801234567890"
    assert record.mobile_phone == "1234567890"
    assert record.mobile_phone_international == "+8801234567890"


def test_parse_customer_extracts_address():
    record = parse_customer_html(_build_html(), "10000001")
    assert record.address == "Test Area"
    assert record.city == "Dhaka"
    assert record.postal_code == "1207"
    assert record.country == "Bangladesh"


def test_parse_customer_extracts_language():
    record = parse_customer_html(_build_html(), "10000001")
    assert record.language == "English"
    assert record.spoken_language == "English"


def test_parse_customer_extracts_registration_date():
    record = parse_customer_html(_build_html(), "10000001")
    assert record.registration_date == "01/01/2025"


def test_select_placeholder_blanked():
    """`Select...` placeholder should be treated as empty, not the literal text."""
    record = parse_customer_html(_build_html(), "10000001")
    assert record.nationality == ""


def test_overrides_propagate():
    """Sanity-check the test helper itself."""
    html = _build_html(
        txtFirstName="Alice", txtLastName="Wonderland",
        ddlLanguage="Bengali",
    )
    record = parse_customer_html(html, "10000002")
    assert record.first_name == "Alice"
    assert record.last_name == "Wonderland"
    assert record.language == "Bengali"


def test_not_found_raises_customer_not_found():
    """An HTML page that lacks every customer-form anchor → CustomerNotFoundError."""
    html = "<html><body>No such customer</body></html>"
    with pytest.raises(CustomerNotFoundError):
        parse_customer_html(html, "99999999")


def test_parse_state_values_finds_admin_and_company_ids():
    landing = """
    <script>
        var stateValues = {
            "ID_ADMIN": "10000",
            "ID_SOCIETE": "2000",
            "ID_APPLICATION": "3",
            "ID_LANGUE": "2",
            "Culture": "en-GB"
        };
    </script>
    """
    state = parse_state_values(landing)
    assert state["ID_ADMIN"] == "10000"
    assert state["ID_SOCIETE"] == "2000"
    assert state["ID_APPLICATION"] == "3"
    assert state["ID_LANGUE"] == "2"
    assert state["Culture"] == "en-GB"


def test_parse_state_values_raises_when_block_missing():
    with pytest.raises(LoginError):
        parse_state_values("<html>nothing here</html>")
