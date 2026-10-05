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


# ---- the emailed verification code (added to sign-in Oct 2026) -------------

_VERIFY_URL = ("https://z.test/Zenith/BackOffice/tti/en-GB/User/"
               "VerificationCode?idCompany=77&idUser=12")
_VERIFY_PAGE = (
    '<form action="/Zenith/BackOffice/tti/en-GB/User/VerificationCode?'
    'idCompany=77&amp;idUser=12" method="post">'
    '<input Value="12" id="ID_User" name="ID_User" type="hidden" value="" />'
    '<input Value="77" id="ID_Company" name="ID_Company" type="hidden" '
    'value="" /><input id="VerificationCode" maxlength="6" '
    'name="VerificationCode" type="text" value="" />'
    'A verification code has been sent to your email : ab*****@example.com'
    '</form>')
_LANDING = ('<script>var stateValues = {"ID_ADMIN": "12", "ID_SOCIETE": '
            '"77", "ID_APPLICATION": "3"};</script>')


class _Resp:
    def __init__(self, url, text=""):
        self.url, self.text, self.status_code = url, text, 200

    def raise_for_status(self):
        pass


class _FakeZenith:
    """Password -> code page; the right code -> the dashboard."""
    posted: list = []

    def __init__(self):
        self.headers = {}

    def post(self, url, data=None, **_kw):
        _FakeZenith.posted.append((url, dict(data or {})))
        if "TESTLOGIN" in url:
            return _Resp(_VERIFY_URL, _VERIFY_PAGE)
        if data.get("VerificationCode") == "XLOHJN":
            return _Resp("https://z.test/NewUI/aerien/f_index.asp", _LANDING)
        return _Resp(_VERIFY_URL, _VERIFY_PAGE)

    def get(self, url, **_kw):
        return _Resp(url)


@pytest.fixture
def fake_zenith(monkeypatch):
    import src.zenith_client as zc
    _FakeZenith.posted = []
    monkeypatch.setattr(zc.requests, "Session", _FakeZenith)
    return zc


def test_sign_in_asks_for_the_emailed_code_and_sends_it(fake_zenith):
    asked = []
    sess = fake_zenith.ZenithSession.from_credentials(
        "u", "p", ask_code=lambda sent, problem: asked.append(
            (sent, problem)) or " xlohjn ")
    assert sess.state_values["ID_ADMIN"] == "12"
    assert asked == [("ab*****@example.com", "")]
    url, form = _FakeZenith.posted[-1]
    assert url == _VERIFY_URL
    assert form == {"ID_User": "12", "ID_Company": "77",
                    "VerificationCode": "XLOHJN"}


def test_a_wrong_code_is_asked_again_with_the_reason(fake_zenith):
    codes = iter(["WRONG1", "XLOHJN"])
    problems = []
    fake_zenith.ZenithSession.from_credentials(
        "u", "p", ask_code=lambda s, p: problems.append(p) or next(codes))
    assert problems[0] == "" and "did not accept" in problems[1]


def test_cancelling_the_code_stops_the_sign_in(fake_zenith):
    with pytest.raises(LoginError, match="cancelled"):
        fake_zenith.ZenithSession.from_credentials(
            "u", "p", ask_code=lambda s, p: None)


def test_without_a_way_to_ask_the_code_page_is_explained(fake_zenith):
    with pytest.raises(LoginError, match="verification code"):
        fake_zenith.ZenithSession.from_credentials("u", "p")
