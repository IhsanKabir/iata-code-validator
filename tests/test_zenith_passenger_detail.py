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
    diagnose_passenger_fetch,
    extract_postback_context,
    fetch_passenger_details,
    fetch_passenger_details_postback,
    parse_passenger_form,
    parse_traveler_update_html,
    traveler_update_url,
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
    """Serves queued responses. Each item is a str (200) or a (status, text) tuple."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.posted: list = []
        self.session = self
        self.state_values: dict = {}      # ZenithSession carries these

    def _next(self, url):
        item = self._responses.pop(0) if self._responses else ""
        status, text = item if isinstance(item, tuple) else (200, item)

        class _R:
            pass
        r = _R()
        r.text = text
        r.status_code = status
        r.url = url
        return r

    def post(self, url, data=None, timeout=None, allow_redirects=True, headers=None):
        self.posted.append(dict(data or {}))
        return self._next(url)

    def get(self, url, timeout=None, allow_redirects=True, headers=None):
        self.posted.append({"GET": url})
        return self._next(url)


_PAX_FORM_2 = (
    '<div>AD Mr. JESUN HAQUE</div>'
    '<input name="m$txtFirstName" value="JESUN"><input name="m$txtLastName" value="HAQUE">'
    '<input name="m$txtDocumentNumber" value="BX9990001">'
)


def test_fetch_passenger_details_posts_each_target_and_parses():
    sess = _FakeSession([_PAX_FORM, _PAX_FORM_2])   # two DIFFERENT passengers
    out = fetch_passenger_details_postback(sess, _DOSSIER, "https://z/Sales/Dossier.aspx?x=1",
                                  pnr="0A1DEA")
    assert len(out) == 2
    assert all(isinstance(p, PassengerDetail) for p in out)
    assert {p.document_number for p in out} == {"EP8057773", "BX9990001"}
    assert sess.posted[0]["__EVENTTARGET"] == "a$rptPassagers$ctl01$linkPassager"
    assert sess.posted[0]["__VIEWSTATE"] == "VS-DATA=="
    assert sess.posted[1]["__EVENTTARGET"] == "a$rptPassagers$ctl02$linkPassager"


def test_fetch_passenger_details_skips_bad_responses():
    sess = _FakeSession(["<html>no fields</html>", _PAX_FORM])
    out = fetch_passenger_details_postback(sess, _DOSSIER, "https://z/Dossier.aspx")
    assert len(out) == 1 and out[0].document_number == "EP8057773"


def test_fetch_passenger_details_empty_when_no_targets():
    dossier_no_pax = '<form action="Dossier.aspx"></form>'
    assert fetch_passenger_details_postback(_FakeSession([]), dossier_no_pax, "u") == []


# --- weaknesses found in self-review, now fixed --------------------------------

_ROUND_TRIP_DOSSIER = """
<form action="Dossier.aspx?taskId=z">
<input type="hidden" name="__VIEWSTATE" value="VS">
<a href="javascript:__doPostBack('a$rptSegments$ctl00$rptPassagers$ctl01$linkPassager','')">YU ZHENJIE</a>
<a href="javascript:__doPostBack('a$rptSegments$ctl01$rptPassagers$ctl01$linkPassager','')">YU ZHENJIE</a>
</form>
"""


def test_round_trip_passenger_deduped_by_name():
    # Same passenger appears once per segment on a round trip — must POST ONCE.
    ctx = extract_postback_context(_ROUND_TRIP_DOSSIER)
    assert len(ctx.passenger_targets) == 1        # deduped by name, not 2
    assert "ctl00$rptPassagers" in ctx.passenger_targets[0]


def test_output_dedup_by_identity():
    # Even if two targets slip through, identical people collapse to one row.
    sess = _FakeSession([_PAX_FORM, _PAX_FORM])
    dossier = _DOSSIER  # two DISTINCT targets, but both return the same person
    out = fetch_passenger_details_postback(sess, dossier, "https://z/Dossier.aspx")
    assert len(out) == 1                          # same passport/name -> one row


def test_postback_retries_5xx_then_succeeds():
    # A transient 504 on the postback must be retried, not silently lost.
    sess = _FakeSession([(504, "gateway timeout"), (200, _PAX_FORM)])
    # single-passenger dossier so only one target is posted
    one_pax = _DOSSIER.replace(
        '<a id="x_rptPassagers_ctl02_linkPassager"\n'
        '   href="javascript:__doPostBack(\'a$rptPassagers$ctl02$linkPassager\',\'\')">JESUN HAQUE</a>',
        "")
    out = fetch_passenger_details_postback(sess, one_pax, "https://z/Dossier.aspx")
    assert len(out) == 1 and out[0].document_number == "EP8057773"
    assert len(sess.posted) == 2                  # first 504, retried to 200


def test_diagnostic_reports_no_modern_idpnr(tmp_path):
    lines = diagnose_passenger_fetch(
        _FakeSession([]), "<html>legacy dossier, no idPNR</html>", "https://z/x",
        pnr="X", out_dir=str(tmp_path))
    joined = " ".join(lines)
    assert "no modern idPNR" in joined
    assert (tmp_path / "_paxdiag_X_dossier.html").exists()   # saved for inspection


def test_diagnostic_reports_modern_parse_and_saves(tmp_path):
    sess = _FakeSession([_TRAVELER_UPDATE])
    lines = diagnose_passenger_fetch(
        sess, _DOSSIER_MODERN,
        "https://asia.ttinteractive.com/TTIDotNet/x/Dossier.aspx",
        pnr="0A1CDT", out_dir=str(tmp_path))
    joined = " ".join(lines)
    assert "PARSED OK" in joined and "EP8057773" in joined
    assert (tmp_path / "_paxdiag_0A1CDT_A_direct_get.html").exists()


# --- MODERN path: passenger detail moved to BackOffice Traveler/Update ----------

_DOSSIER_MODERN = (
    '<html>… /Zenith/BackOffice/USBangla/en-GB/Traveler/UpdatePassengersGroupID?idPNR=16858865 …'
    '<a href="javascript:__doPostBack(\'a$rptPassagers$ctl01$linkPassager\',\'\')">YU ZHENJIE</a>'
    '</html>'
)

# Two travelers in the MVC Travelers[N] model (mirrors the real Traveler/Update page).
_TRAVELER_UPDATE = """
<input name="Travelers[0].Surname" type="text" value="YU" />
<input name="Travelers[0].Firstname" type="text" value="ZHENJIE" />
<select name="Travelers[0].Civility"><option value="0">Select...</option><option selected="selected" value="1">Mr.</option></select>
<select name="Travelers[0].Gender"><option selected="selected">Male</option></select>
<input name="Travelers[0].DateOfBirth.Day" type="text" value="1" />
<select name="Travelers[0].DateOfBirth.Month"><option selected="selected">September</option></select>
<input name="Travelers[0].DateOfBirth.Year" type="text" value="1973" />
<select name="Travelers[0].Nationality"><option selected="selected">China</option></select>
<input name="Travelers[0].Email" type="text" value="1187435137@qq.com" />
<input name="Travelers[0].MobilePhoneNumber" type="tel" value="18188806906" />
<select name="Travelers[0].DocumentType"><option selected="selected">Passport</option></select>
<input name="Travelers[0].DocumentNumber" type="text" value="EP8057773" />
<input name="Travelers[0].DocumentExpirationDate" type="datetime" value="05/06/2035" />
<select name="Travelers[0].DocumentIssuingCountry"><option selected="selected">Bangladesh</option></select>
<input name="Travelers[1].Surname" type="text" value="HAQUE" />
<input name="Travelers[1].Firstname" type="text" value="JESUN" />
<input name="Travelers[1].DocumentNumber" type="text" value="BX9990001" />
"""


def test_traveler_update_url_built_from_dossier():
    url = traveler_update_url(
        _DOSSIER_MODERN,
        "https://asia.ttinteractive.com/TTIDotNet/Transport/TransportNetBO2/Sales/Dossier.aspx?x=1")
    assert url.startswith(
        "https://asia.ttinteractive.com/Zenith/BackOffice/USBangla/en-GB/Traveler/Update?idPNR=16858865")
    assert "backURL=%252F" in url or "backURL=%252f" in url   # double-encoded return url


def test_traveler_update_url_none_without_idpnr():
    assert traveler_update_url("<html>legacy dossier, no idPNR</html>", "https://z/x") is None


def test_parse_traveler_update_all_fields_and_multi_pax():
    pax = parse_traveler_update_html(_TRAVELER_UPDATE, pnr="0A1CDT")
    assert len(pax) == 2                                       # both travelers
    a = pax[0]
    assert a.last_name == "YU" and a.first_name == "ZHENJIE"
    assert a.title == "Mr." and a.gender == "Male"
    assert a.date_of_birth == "1/September/1973"
    assert a.nationality == "China" and a.email == "1187435137@qq.com"
    assert a.mobile_phone == "18188806906"
    assert a.document_type == "Passport" and a.document_number == "EP8057773"
    assert a.document_expiry == "05/06/2035" and a.document_country == "Bangladesh"
    assert pax[1].last_name == "HAQUE" and pax[1].document_number == "BX9990001"


def test_fetch_passenger_details_modern_one_get_all_pax():
    sess = _FakeSession([_TRAVELER_UPDATE])
    out = fetch_passenger_details(
        sess, _DOSSIER_MODERN, "https://asia.ttinteractive.com/TTIDotNet/x/Dossier.aspx",
        pnr="0A1CDT")
    assert len(out) == 2                                       # ONE GET -> all passengers
    assert out[0].document_number == "EP8057773"
    assert len(sess.posted) == 1 and "GET" in sess.posted[0]   # a single GET, no postbacks


def test_fetch_passenger_details_modern_empty_without_idpnr():
    sess = _FakeSession([_TRAVELER_UPDATE])
    assert fetch_passenger_details(sess, "<html>legacy, no idPNR</html>", "u") == []


def test_fetch_modern_self_heals_via_pollsession():
    # First Traveler/Update -> BackOffice error page; PollSession bootstraps the
    # modern session; retry -> the real form. (The reported cross-sell bug.)
    sess = _FakeSession([
        "<html>ERROR PAGE — session not accepted</html>",   # 1st Traveler/Update
        "PollSession Successful. SessionID=abc",             # PollSession bootstrap
        _TRAVELER_UPDATE,                                    # retried Traveler/Update
    ])
    sess.state_values = {"ID_ADMIN": "37739", "ID_SOCIETE": "2035"}
    out = fetch_passenger_details(
        sess, _DOSSIER_MODERN,
        "https://asia.ttinteractive.com/TTIDotNet/x/Dossier.aspx", pnr="0A1CDT")
    assert len(out) == 2 and out[0].document_number == "EP8057773"
    assert len(sess.posted) == 3                    # traveler(err) + poll + traveler(retry)
    assert "BookingEngine/PollSession" in sess.posted[1]["GET"]
    assert "idUser=37739" in sess.posted[1]["GET"]


def test_establish_backoffice_needs_state_values():
    from src.zenith_passenger import establish_backoffice_session
    sess = _FakeSession(["PollSession Successful."])
    sess.state_values = {}                          # no ID_ADMIN -> can't build
    assert establish_backoffice_session(sess, _DOSSIER_MODERN, "https://z/x") is False


# --- browser-faithful postback -> 302 fallback (validated vs real HAR) ---------

_DOSSIER_MODERN_FULL = (
    '<html>… /Zenith/BackOffice/USBangla/en-GB/Traveler/Update?idPNR=16858865 …'
    '<form name="Form1" method="post" action="Dossier.aspx?view=UsrDossierSynthese&amp;taskId=t1">'
    '<input type="hidden" name="__VIEWSTATE" value="VS==" />'
    '<input type="hidden" name="__EVENTVALIDATION" value="EV==" />'
    '<input type="hidden" name="PageInstance" value="pi-1" />'
    '<input type="text" name="instanceCtrlContent$tbNom" value="YU" />'
    '<input type="text" name="txtDisabled" value="Z" disabled />'
    '<select name="instanceCtrlContent$drpDevise"><option value="0">Select</option>'
    '<option selected="selected" value="BDT">BDT</option></select>'
    '<select name="instanceCtrlContent$drpFirst"><option value="A">A</option>'
    '<option value="B">B</option></select>'
    '<select name="instanceCtrlContent$drpEmpty"></select>'
    '<select name="drpDisabled" disabled><option selected value="X">X</option></select>'
    '<textarea name="instanceCtrlContent$tbComment">hi there</textarea>'
    '<input type="submit" name="btnGo" value="Go" />'
    '<input type="checkbox" name="chkOff" value="1" />'
    '<input type="checkbox" name="chkOn" value="1" checked />'
    '<a href="javascript:__doPostBack(\'instanceCtrlContent$rptPassagers$ctl01$linkPassager\',\'\')">YU ZHENJIE</a>'
    '</form></html>'
)


def test_harvest_form_fields_matches_browser_serialization():
    from src.zenith_passenger import harvest_form_fields
    f = harvest_form_fields(_DOSSIER_MODERN_FULL)
    assert f["__VIEWSTATE"] == "VS==" and f["__EVENTVALIDATION"] == "EV=="
    assert f["PageInstance"] == "pi-1"
    assert f["instanceCtrlContent$tbNom"] == "YU"
    assert f["instanceCtrlContent$drpDevise"] == "BDT"        # selected option value
    assert f["instanceCtrlContent$drpFirst"] == "A"           # first-option default
    assert f["instanceCtrlContent$tbComment"] == "hi there"   # textarea inner text
    assert f["chkOn"] == "1"                                  # checked box included
    assert "chkOff" not in f                                  # unchecked box omitted
    assert "btnGo" not in f                                   # submit button omitted
    assert "txtDisabled" not in f and "drpDisabled" not in f  # disabled omitted
    assert "instanceCtrlContent$drpEmpty" not in f            # AJAX-empty select omitted


def test_build_passenger_postback_body_sets_target():
    from src.zenith_passenger import build_passenger_postback_body
    body = build_passenger_postback_body(
        _DOSSIER_MODERN_FULL, "instanceCtrlContent$rptPassagers$ctl01$linkPassager")
    assert body["__EVENTTARGET"] == "instanceCtrlContent$rptPassagers$ctl01$linkPassager"
    assert body["__EVENTARGUMENT"] == ""
    assert body["__VIEWSTATE"] == "VS=="                      # full form carried along


def test_fetch_falls_back_to_postback_when_pollsession_insufficient():
    # Direct GET -> error; PollSession succeeds but retry GET STILL errors (session
    # alone insufficient); browser-faithful postback -> 302 -> modern form works.
    sess = _FakeSession([
        "<html>ERROR PAGE</html>",             # A: direct GET
        "PollSession Successful.",             # A+: PollSession bootstrap
        "<html>ERROR PAGE still</html>",       # A+: retry GET (still refused)
        _TRAVELER_UPDATE,                      # B: postback POST -> followed 302 form
    ])
    sess.state_values = {"ID_ADMIN": "37739", "ID_SOCIETE": "2035"}
    out = fetch_passenger_details(
        sess, _DOSSIER_MODERN_FULL,
        "https://asia.ttinteractive.com/TTIDotNet/x/Dossier.aspx", pnr="0A1CDT")
    assert len(out) == 2 and out[0].document_number == "EP8057773"
    assert sess._pax_strategy == "postback"                   # winner cached
    assert len(sess.posted) == 4
    # the 4th call is the postback POST carrying the passenger target + full form
    post = sess.posted[3]
    assert post["__EVENTTARGET"].endswith("linkPassager")
    assert post["__VIEWSTATE"] == "VS=="


def test_cached_postback_strategy_skips_direct_get_on_next_pnr():
    sess = _FakeSession([_TRAVELER_UPDATE])   # only a postback response queued
    sess.state_values = {"ID_ADMIN": "37739", "ID_SOCIETE": "2035"}
    sess._pax_strategy = "postback"           # learned from a previous PNR
    sess._bo_ready = True                     # modern session already bootstrapped
    out = fetch_passenger_details(
        sess, _DOSSIER_MODERN_FULL,
        "https://asia.ttinteractive.com/TTIDotNet/x/Dossier.aspx", pnr="0A1CDU")
    assert len(out) == 2
    assert len(sess.posted) == 1 and "__EVENTTARGET" in sess.posted[0]  # straight to postback


def test_successful_direct_get_caches_direct_strategy():
    sess = _FakeSession([_TRAVELER_UPDATE])
    fetch_passenger_details(
        sess, _DOSSIER_MODERN, "https://asia.ttinteractive.com/TTIDotNet/x/Dossier.aspx",
        pnr="0A1CDT")
    assert sess._pax_strategy == "direct"


def test_parse_traveler_update_input_mode_civility_and_gender():
    # Some PNRs render Civility/Gender as read-only INPUTS, not selects: Gender as
    # text, Civility as a numeric ID (mapping from the page's own select options).
    html = (
        '<input name="Travelers[0].Surname" value="HOSSAIN" />'
        '<input name="Travelers[0].Firstname" value="MD AKTAR" />'
        '<input name="Travelers[0].Civility" value="15" />'
        '<input name="Travelers[0].Gender" value="Female" />'
        '<input name="Travelers[1].Surname" value="AKTER" />'
        '<input name="Travelers[1].Civility" value="99" />'   # unknown ID -> blank
        '<input name="Travelers[2].Surname" value="MIA" />'
        '<input name="Travelers[2].Civility" value="0" />'    # 0 = Select... -> blank
    )
    pax = parse_traveler_update_html(html, pnr="0A1CDT")
    assert pax[0].title == "Mstr." and pax[0].gender == "Female"
    assert pax[1].title == "" and pax[2].title == ""


def test_parse_traveler_update_select_mode_still_wins():
    # Select-mode pages keep working; select takes precedence over any stray input.
    html = (
        '<input name="Travelers[0].Surname" value="YU" />'
        '<select name="Travelers[0].Civility"><option value="0">Select...</option>'
        '<option selected="selected" value="1">Mr.</option></select>'
        '<select name="Travelers[0].Gender"><option selected>Male</option></select>'
    )
    pax = parse_traveler_update_html(html)
    assert pax[0].title == "Mr." and pax[0].gender == "Male"
