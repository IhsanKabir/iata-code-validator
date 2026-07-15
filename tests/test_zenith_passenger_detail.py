"""Tests for per-passenger DETAIL extraction (postback replay + form parse).

Distinct from test_zenith_passenger.py (the flight-manifest parser): this covers
zenith_passenger.py, which replays the passenger `__doPostBack` on a dossier and
reads the returned Customer-style form (email, phones, doc expiry, issuing
country, FFP). Pure functions are covered offline; the POST uses a fake session.
Synthetic HTML mirrors Zenith's ASP.NET WebForms markup — no real PII.
"""

from __future__ import annotations

from src.zenith_passenger import (
    PassengerDetail,
    extract_postback_context,
    fetch_passenger_details,
    parse_passenger_form,
)

_DOSSIER = """
<html><form name="Form1" method="post"
 action="Dossier.aspx?view=UsrDossierSynthese&amp;taskId=abc-123&amp;ViewName=X">
<input type="hidden" name="__VIEWSTATE" value="VS-DATA==" />
<input type="hidden" name="__VIEWSTATEGENERATOR" value="A1B2" />
<input type="hidden" name="__EVENTVALIDATION" value="EV-DATA==" />
<input type="hidden" name="__EVENTTARGET" value="" />
<input type="hidden" name="__EVENTARGUMENT" value="" />
<a id="x_rptPassagers_ctl01_linkPassager"
   href="javascript:__doPostBack('a$rptPassagers$ctl01$linkPassager','')">YU ZHENJIE</a>
<a id="x_rptPassagers_ctl02_linkPassager"
   href="javascript:__doPostBack('a$rptPassagers$ctl02$linkPassager','')">JESUN HAQUE</a>
</form></html>
"""

_PAX_FORM = """
<div class="header">AD Mr. YU ZHENJIE</div>
<select name="m$ddlTitle"><option>Select...</option><option selected>Mr.</option></select>
<select name="m$ddlGender"><option selected>Male</option></select>
<input name="m$txtLastName" value="YU">
<input name="m$txtFirstName" value="ZHENJIE">
<input name="m$txtDateOfBirth" value="01/09/1973">
<select name="m$ddlNationality"><option selected>China</option></select>
<input name="m$txtEmail" value="1187435137@qq.com">
<input name="m$txtHomePhoneNumber" value="18188806906">
<input name="m$txtMobilePhoneNumber" value="18188806906">
<select name="m$ddlDocumentType"><option selected>Passport</option></select>
<input name="m$txtDocumentNumber" value="EP8057773">
<input name="m$txtDocumentExpirationDate" value="05/06/2035">
<select name="m$ddlDocumentIssuingCountry"><option selected>Bangladesh</option></select>
<input name="m$txtFFPNumber" value="BS12345">
"""


def test_extract_postback_context():
    ctx = extract_postback_context(_DOSSIER)
    assert ctx.action.startswith("Dossier.aspx")
    assert "&amp;" not in ctx.action                 # entities decoded
    assert ctx.hidden["__VIEWSTATE"] == "VS-DATA=="
    assert ctx.hidden["__EVENTVALIDATION"] == "EV-DATA=="
    assert ctx.passenger_targets == (
        "a$rptPassagers$ctl01$linkPassager",
        "a$rptPassagers$ctl02$linkPassager",
    )


def test_parse_passenger_form_maps_every_field():
    d = parse_passenger_form(_PAX_FORM, pnr="0A1DEA", index=1)
    assert d is not None
    assert d.pnr == "0A1DEA" and d.passenger_index == 1
    assert d.header_name == "AD Mr. YU ZHENJIE"
    assert d.title == "Mr." and d.gender == "Male"
    assert d.first_name == "ZHENJIE" and d.last_name == "YU"
    assert d.date_of_birth == "01/09/1973"
    assert d.nationality == "China"
    assert d.email == "1187435137@qq.com"
    assert d.home_phone == "18188806906" and d.mobile_phone == "18188806906"
    assert d.document_type == "Passport"
    assert d.document_number == "EP8057773"
    assert d.document_expiry == "05/06/2035"
    assert d.document_country == "Bangladesh"
    assert d.ffp_number == "BS12345"
    assert d.raw_fields["txtdocumentnumber"] == "EP8057773"   # nothing dropped


def test_parse_passenger_form_french_leaf_names():
    fr = (
        '<input name="m$txtPrenom" value="ZHENJIE">'
        '<input name="m$txtNom" value="YU">'
        '<select name="m$ddlNationalite"><option selected>Chine</option></select>'
        '<input name="m$txtNumeroDocument" value="EP8057773">'
        '<input name="m$txtDateExpiration" value="05/06/2035">'
    )
    d = parse_passenger_form(fr)
    assert d.first_name == "ZHENJIE" and d.last_name == "YU"
    assert d.nationality == "Chine"
    assert d.document_number == "EP8057773"
    assert d.document_expiry == "05/06/2035"


def test_parse_passenger_form_returns_none_for_non_form():
    assert parse_passenger_form("<html>dashboard, no fields</html>") is None


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.posted: list = []
        self.session = self

    def post(self, url, data=None, timeout=None, allow_redirects=True):
        self.posted.append(data)
        text = self._responses.pop(0) if self._responses else ""

        class _R:
            pass
        r = _R()
        r.text = text
        return r


def test_fetch_passenger_details_posts_each_target_and_parses():
    sess = _FakeSession([_PAX_FORM, _PAX_FORM])
    out = fetch_passenger_details(sess, _DOSSIER, "https://z/Sales/Dossier.aspx?x=1",
                                  pnr="0A1DEA")
    assert len(out) == 2
    assert all(isinstance(p, PassengerDetail) for p in out)
    assert out[0].document_number == "EP8057773"
    assert sess.posted[0]["__EVENTTARGET"] == "a$rptPassagers$ctl01$linkPassager"
    assert sess.posted[0]["__VIEWSTATE"] == "VS-DATA=="
    assert sess.posted[1]["__EVENTTARGET"] == "a$rptPassagers$ctl02$linkPassager"


def test_fetch_passenger_details_skips_bad_responses():
    sess = _FakeSession(["<html>no fields</html>", _PAX_FORM])
    out = fetch_passenger_details(sess, _DOSSIER, "https://z/Dossier.aspx")
    assert len(out) == 1 and out[0].document_number == "EP8057773"


def test_fetch_passenger_details_empty_when_no_targets():
    dossier_no_pax = '<form action="Dossier.aspx"></form>'
    assert fetch_passenger_details(_FakeSession([]), dossier_no_pax, "u") == []
