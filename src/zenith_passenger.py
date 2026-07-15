"""Per-passenger detail extraction from a Zenith dossier (PNR) page.

The passenger detail form (passport no., expiry, issuing country, nationality,
email, phones, FFP) is NOT in the dossier HTML — clicking a passenger name fires
an ASP.NET `__doPostBack`. This module replays that postback: it reads the hidden
form state (`__VIEWSTATE` / `__VIEWSTATEGENERATOR` / `__EVENTVALIDATION`) plus the
per-passenger event target out of the dossier HTML the PNR lookup already fetched,
POSTs it back, and parses the returned Customer-style form.

The form parser is deliberately GENERIC: it captures every `txt*` text field and
`ddl*` dropdown by name (the same trick the customer parser uses), then maps them
to named columns by keyword — so unknown English/French field names (e.g.
`txtNumeroDocument`) are still captured. Everything is also kept in `raw_fields`
so nothing is ever silently dropped.

No network in the pure functions (`extract_postback_context`, `parse_passenger_form`)
— they are fully unit-tested offline. `fetch_passenger_details` does the POSTs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

log = logging.getLogger(__name__)

# Reuse the exact markup the customer parser matches: `name="…$txtXxx" value="…"`
# and `<select name="…$ddlXxx">…<option selected>`.
_TXT_RE = re.compile(r'name="[^"]*\$(txt[A-Za-z]+)"[^>]*?\bvalue="([^"]*)"', re.IGNORECASE)
_SEL_RE = re.compile(
    r'<select[^>]+name="[^"]*\$(ddl[A-Za-z]+)"[^>]*>(.*?)</select>',
    re.IGNORECASE | re.DOTALL)
_SEL_OPT_RE = re.compile(r'<option[^>]*\bselected[^>]*>([^<]*)</option>', re.IGNORECASE)
_HIDDEN_RE = re.compile(
    r'<input[^>]*type="hidden"[^>]*>', re.IGNORECASE)
_NAME_RE = re.compile(r'\bname="([^"]*)"')
_VALUE_RE = re.compile(r'\bvalue="([^"]*)"')
_FORM_ACTION_RE = re.compile(r'<form[^>]*\baction="([^"]*)"', re.IGNORECASE)
_PAX_TARGET_RE = re.compile(r"__doPostBack\('([^']*linkPassager)'")
# Target + the passenger NAME in the anchor text — used to dedup a passenger who
# appears once PER SEGMENT on a round trip (rptSegments$ctl00 AND $ctl01).
_PAX_TARGET_NAME_RE = re.compile(
    r"__doPostBack\('([^']*linkPassager)','[^']*'\)\"[^>]*>([^<]+)</a>")
# The form header renders the passenger as "AD Mr. YU ZHENJIE" in a green bar.
_PAX_HEADER_RE = re.compile(
    r'(?:AD|CHD?|INF)\s+(?:Mr|Mrs|Ms|Mstr|Miss|Dr)\.?\s+[A-Z][A-Za-z .\'-]+')


@dataclass(frozen=True)
class PostbackContext:
    action: str                       # form action (relative or absolute)
    hidden: dict[str, str]            # every hidden input name->value
    passenger_targets: tuple[str, ...]  # __EVENTTARGET per passenger link


@dataclass(frozen=True)
class PassengerDetail:
    pnr: str = ""
    passenger_index: int = 0
    header_name: str = ""             # "AD Mr. YU ZHENJIE" from the form banner
    title: str = ""
    gender: str = ""
    first_name: str = ""
    last_name: str = ""
    date_of_birth: str = ""
    nationality: str = ""
    email: str = ""
    home_phone: str = ""
    mobile_phone: str = ""
    document_type: str = ""
    document_number: str = ""
    document_expiry: str = ""
    document_country: str = ""
    ffp_number: str = ""
    raw_fields: dict[str, str] = field(default_factory=dict)  # every txt*/ddl* found


def extract_postback_context(dossier_html: str) -> PostbackContext:
    """Pull the ASP.NET form state + per-passenger event targets out of a dossier."""
    m = _FORM_ACTION_RE.search(dossier_html)
    action = unescape(m.group(1)) if m else ""
    hidden: dict[str, str] = {}
    for tag in _HIDDEN_RE.findall(dossier_html):
        nm = _NAME_RE.search(tag)
        if not nm:
            continue
        vm = _VALUE_RE.search(tag)
        hidden[nm.group(1)] = unescape(vm.group(1)) if vm else ""
    # Dedup by passenger NAME so a round-trip passenger (listed per segment) is
    # fetched once. Fall back to raw-target dedup if names aren't capturable.
    pairs = _PAX_TARGET_NAME_RE.findall(dossier_html)
    if pairs:
        seen: set[str] = set()
        targets_list: list[str] = []
        for target, name in pairs:
            key = " ".join(name.split()).upper()
            if key in seen:
                continue
            seen.add(key)
            targets_list.append(target)
        targets = tuple(targets_list)
    else:
        targets = tuple(dict.fromkeys(_PAX_TARGET_RE.findall(dossier_html)))
    return PostbackContext(action=action, hidden=hidden, passenger_targets=targets)


def _selected(block: str) -> str:
    m = _SEL_OPT_RE.search(block)
    v = unescape(m.group(1)).strip() if m else ""
    return "" if v in ("Select...", "Select…") else v


def _collect_fields(html: str) -> dict[str, str]:
    """Every txt* text value + ddl* selected value, keyed by lowercased leaf name."""
    out: dict[str, str] = {}
    for name, value in _TXT_RE.findall(html):
        out[name.lower()] = unescape(value).strip()
    for name, block in _SEL_RE.findall(html):
        out[name.lower()] = _selected(block)
    return out


def _pick(fields: dict[str, str], *keyword_groups: tuple[str, ...],
          exclude: tuple[str, ...] = ()) -> str:
    """First non-empty field whose leaf name contains ALL keywords in any group
    and NONE of `exclude`. Groups are tried in order (specific first). Case-
    insensitive and language-agnostic (English or French leaf names). `exclude`
    stops greedy substrings — e.g. French 'nom' (surname) must not match the
    'nom' inside 'prenom' (given name)."""
    for group in keyword_groups:
        for leaf, val in fields.items():
            if val and all(k in leaf for k in group) and not any(x in leaf for x in exclude):
                return val
    return ""


def parse_passenger_form(html: str, *, pnr: str = "", index: int = 0) -> PassengerDetail | None:
    """Map a passenger/customer detail form into a PassengerDetail.

    Returns None if the response doesn't look like a passenger form (no name
    fields) — e.g. the postback re-rendered the dossier instead."""
    fields = _collect_fields(html)
    first = _pick(fields, ("firstname",), ("prenom",))
    last = _pick(fields, ("lastname",), ("nom",), exclude=("prenom",))
    if not first and not last and "txtnom" not in fields and "txtlastname" not in fields:
        return None  # not a passenger form
    header = ""
    hm = _PAX_HEADER_RE.search(re.sub(r"<[^>]+>", " ", html))
    if hm:
        header = " ".join(hm.group(0).split())
    return PassengerDetail(
        pnr=pnr, passenger_index=index, header_name=header,
        title=_pick(fields, ("title",), ("civilite",)),
        gender=_pick(fields, ("gender",), ("sexe",), ("genre",)),
        first_name=first, last_name=last,
        date_of_birth=_pick(fields, ("dateofbirth",), ("birth",), ("naissance",), ("dob",)),
        nationality=_pick(fields, ("nationalit",)),
        email=_pick(fields, ("email",), ("courriel",)),
        home_phone=_pick(fields, ("home", "phone"), ("domicile",), ("fixe",)),
        mobile_phone=_pick(fields, ("mobile", "phone"), ("mobile",), ("portable",), ("cell",)),
        document_type=_pick(fields, ("documenttype",), ("typedocument",),
                            ("typepiece",), ("pieceidentite",)),
        document_number=_pick(fields, ("documentnumber",), ("numerodocument",),
                              ("numdocument",), ("passport",), ("passeport",),
                              ("numeropiece",), ("numpiece",), ("document", "num")),
        document_expiry=_pick(fields, ("documentexpir",), ("dateexpir",), ("expir",),
                              ("validit",), ("dateexpiration",)),
        document_country=_pick(fields, ("pays", "emis"), ("pays", "delivr"),
                               ("country", "issu"), ("issuing",),
                               ("documentcountry",), ("paysdocument",)),
        ffp_number=_pick(fields, ("ffp",), ("fidelisation",), ("fidelite",),
                         ("frequent",), ("miles",)),
        raw_fields=fields,
    )


# --- MODERN path: the passenger detail moved to the BackOffice MVC app --------
# Clicking a passenger now loads Traveler/Update?idPNR=<Id_Dossier>&backURL=…, one
# GET that returns ALL travelers as a Travelers[N] model (verified from a HAR). This
# replaces the dead legacy __doPostBack. idPNR == the dossier's Id_Dossier.
_IDPNR_RE = re.compile(r"idPNR=(\d+)", re.IGNORECASE)
_MODERN_PREFIX_RE = re.compile(
    r"(/Zenith/BackOffice/[^/\"'<> ]+/[^/\"'<> ]+/)Traveler", re.IGNORECASE)
_TRAV_IDX_RE = re.compile(r"Travelers\[(\d+)\]\.Surname")


def traveler_update_url(dossier_html: str, dossier_url: str) -> str | None:
    """Build the modern Traveler/Update URL from the dossier (idPNR + BackOffice
    locale prefix), or None if the dossier has no modern idPNR."""
    m = _IDPNR_RE.search(dossier_html)
    if not m:
        return None
    id_pnr = m.group(1)
    from urllib.parse import quote, urlsplit
    pm = _MODERN_PREFIX_RE.search(dossier_html)
    prefix = pm.group(1) if pm else "/Zenith/BackOffice/USBangla/en-GB/"
    base = f"{urlsplit(dossier_url).scheme}://{urlsplit(dossier_url).netloc}"
    inner = f"/TTIDotNet/Transport/TransportNetBO2/Sales/SyntheseDossier.aspx?Id_Dossier={id_pnr}"
    back = quote(quote(inner, safe=""), safe="")   # double-encoded, as the UI does
    return f"{base}{prefix}Traveler/Update?idPNR={id_pnr}&backURL={back}"


def _trav_text(html: str, idx: int, field: str) -> str:
    m = re.search(
        r'name="Travelers\[' + str(idx) + r'\]\.' + re.escape(field) +
        r'"[^>]*?\bvalue="([^"]*)"', html, re.IGNORECASE)
    return unescape(m.group(1)).strip() if m else ""


# Traveler/Update renders Civility + Gender as a <select> on some PNRs and as a
# plain <input value="…"> on others (read-only mode). The input carries Gender as
# text ("Male") but Civility as a numeric ID — mapping taken from the real page's
# own select options.
_CIVILITY_LABELS = {
    "1": "Mr.", "2": "Mrs.", "3": "Miss", "10": "Dr.", "11": "Pr.",
    "12": "Eng.", "13": "Rev.", "14": "Ms.", "15": "Mstr.", "16": "Hon",
    "17": "Capt.",
}


def _civility_label(raw: str) -> str:
    if raw.isdigit():
        return _CIVILITY_LABELS.get(raw, "")
    return raw


def _trav_select(html: str, idx: int, field: str) -> str:
    blk = re.search(
        r'<select[^>]*name="Travelers\[' + str(idx) + r'\]\.' + re.escape(field) +
        r'"[^>]*>(.*?)</select>', html, re.IGNORECASE | re.DOTALL)
    if not blk:
        return ""
    opt = re.search(r"<option[^>]*\bselected[^>]*>([^<]*)</option>", blk.group(1),
                    re.IGNORECASE)
    v = unescape(opt.group(1)).strip() if opt else ""
    return "" if v in ("Select...", "Select…") else v


def parse_traveler_update_html(html: str, *, pnr: str = "") -> list[PassengerDetail]:
    """Parse the modern Traveler/Update page (Travelers[N] model) into details."""
    out: list[PassengerDetail] = []
    for idx in sorted({int(i) for i in _TRAV_IDX_RE.findall(html)}):
        surname = _trav_text(html, idx, "Surname")
        first = _trav_text(html, idx, "Firstname")
        if not surname and not first:
            continue
        day = _trav_text(html, idx, "DateOfBirth.Day")
        month = _trav_select(html, idx, "DateOfBirth.Month") or _trav_text(html, idx, "DateOfBirth.Month")
        year = _trav_text(html, idx, "DateOfBirth.Year")
        dob = "/".join(x for x in (day, month, year) if x)
        out.append(PassengerDetail(
            pnr=pnr, passenger_index=idx + 1,
            header_name=" ".join(x for x in (surname, first) if x),
            title=_civility_label(_trav_select(html, idx, "Civility")
                                  or _trav_text(html, idx, "Civility")),
            gender=(_trav_select(html, idx, "Gender")
                    or _trav_text(html, idx, "Gender")),
            first_name=first, last_name=surname, date_of_birth=dob,
            nationality=_trav_select(html, idx, "Nationality"),
            email=_trav_text(html, idx, "Email"),
            home_phone=_trav_text(html, idx, "HomePhoneNumber"),
            mobile_phone=_trav_text(html, idx, "MobilePhoneNumber"),
            document_type=_trav_select(html, idx, "DocumentType"),
            document_number=_trav_text(html, idx, "DocumentNumber"),
            document_expiry=_trav_text(html, idx, "DocumentExpirationDate"),
            document_country=_trav_select(html, idx, "DocumentIssuingCountry"),
            ffp_number=(_trav_text(html, idx, "FrequentFlyer.FFPNumber")
                        or _trav_select(html, idx, "FrequentFlyer.FFPLevelId")),
        ))
    return out


_BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
}
_BO_COMPANY_RE = re.compile(r"/Zenith/BackOffice/([^/\"'<> ]+)/", re.IGNORECASE)


# --- browser-faithful postback -> 302 -> modern GET ---------------------------
# The dossier reaches Traveler/Update by POSTing the WHOLE ASP.NET form (a
# `__doPostBack` on a passenger link); the legacy server answers 302 -> the modern
# URL, which the browser then GETs. Verified against a real HAR: rebuilding the
# full form body below reproduces the captured postback field-for-field. This is
# the fallback when a cold GET of the constructed URL isn't accepted.
_TAG_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_TAG_SELECT_RE = re.compile(r"<select\b([^>]*)>(.*?)</select>", re.IGNORECASE | re.DOTALL)
_TAG_TEXTAREA_RE = re.compile(r"<textarea\b([^>]*)>(.*?)</textarea>", re.IGNORECASE | re.DOTALL)
_TAG_OPTION_RE = re.compile(r"<option\b([^>]*)>([^<]*)</option>", re.IGNORECASE)


def _tag_attr(tag: str, name: str) -> str:
    m = re.search(r'\b' + name + r'="([^"]*)"', tag, re.IGNORECASE)
    return unescape(m.group(1)) if m else ""


def _option_value(opt_attrs: str, opt_text: str) -> str:
    """A posted <option> value = its value attr if present, else its text."""
    if re.search(r"\bvalue=", opt_attrs, re.IGNORECASE):
        return _tag_attr(opt_attrs, "value")
    return unescape(opt_text).strip()


def harvest_form_fields(html: str) -> dict[str, str]:
    """Serialize an ASP.NET form exactly as a browser would: every enabled named
    input (checkbox/radio only if checked), each enabled <select> as its selected
    (or first) option value, each enabled <textarea> as its inner text. Disabled
    controls are omitted (browsers don't submit them). Validated field-for-field
    against a real dossier postback."""
    form: dict[str, str] = {}
    for tag in _TAG_INPUT_RE.findall(html):
        name = _tag_attr(tag, "name")
        if not name or "disabled" in tag.lower():
            continue
        itype = (_tag_attr(tag, "type") or "text").lower()
        if itype in ("submit", "button", "image", "reset", "file"):
            continue
        if itype in ("checkbox", "radio") and "checked" not in tag.lower():
            continue
        form[name] = _tag_attr(tag, "value")
    for hdr, block in _TAG_SELECT_RE.findall(html):
        name = _tag_attr(hdr, "name")
        if not name or "disabled" in hdr.lower():
            continue
        opts = _TAG_OPTION_RE.findall(block)
        if not opts:
            continue  # AJAX-empty select: the browser submits nothing
        selected = [(a, t) for a, t in opts if re.search(r"\bselected\b", a, re.IGNORECASE)]
        form[name] = _option_value(*(selected[0] if selected else opts[0]))
    for hdr, inner in _TAG_TEXTAREA_RE.findall(html):
        name = _tag_attr(hdr, "name")
        if name and "disabled" not in hdr.lower():
            form[name] = unescape(re.sub(r"<[^>]+>", "", inner)).strip()
    return form


def build_passenger_postback_body(dossier_html: str, target: str) -> dict[str, str]:
    """The full form body to POST for a passenger `__doPostBack`, i.e. the whole
    serialized dossier form with __EVENTTARGET pointed at the passenger link."""
    body = harvest_form_fields(dossier_html)
    body["__EVENTTARGET"] = target
    body["__EVENTARGUMENT"] = ""
    return body


def _fetch_via_postback(session, dossier_html: str, dossier_url: str,
                        pnr: str, timeout_s: float) -> list[PassengerDetail]:
    """POST the passenger postback to the dossier; the 302 -> modern Traveler/Update
    is followed automatically (allow_redirects), so the response IS the modern form
    with ALL travelers. Any passenger link works — the redirect is PNR-level."""
    ctx = extract_postback_context(dossier_html)
    if not ctx.action or not ctx.passenger_targets:
        return []
    post_url = urljoin(dossier_url, ctx.action)
    body = build_passenger_postback_body(dossier_html, ctx.passenger_targets[0])
    resp = session.session.post(
        post_url, data=body, timeout=timeout_s, allow_redirects=True,
        headers={**_BROWSER_HEADERS, "Referer": dossier_url,
                 "Content-Type": "application/x-www-form-urlencoded",
                 "Origin": f"{dossier_url.split('/TTIDotNet')[0]}"})
    text = getattr(resp, "text", "") or ""
    return parse_traveler_update_html(text, pnr=pnr)


def _ensure_backoffice_session(session, dossier_html: str, dossier_url: str) -> bool:
    """Call PollSession at most once per session (cached), mirroring the browser's
    login-time bootstrap of the modern app."""
    if getattr(session, "_bo_ready", False):
        return True
    ok = establish_backoffice_session(session, dossier_html, dossier_url)
    if ok:
        try:
            session._bo_ready = True
        except Exception:  # noqa: BLE001 — frozen session: just re-derive next time
            pass
    return ok


def _remember_strategy(session, strategy: str) -> None:
    try:
        session._pax_strategy = strategy
    except Exception:  # noqa: BLE001
        pass


def establish_backoffice_session(session, dossier_html: str, dossier_url: str) -> bool:
    """Bootstrap the modern BackOffice session by calling PollSession, exactly as
    the browser does at sign-in. The app's login authenticates the LEGACY app but
    never calls this, so the modern Traveler pages reject the session. idUser /
    idCompany come from the session's state_values (ID_ADMIN / ID_SOCIETE)."""
    from urllib.parse import urlsplit
    m = _BO_COMPANY_RE.search(dossier_html)
    sv = getattr(session, "state_values", None) or {}
    id_user = sv.get("ID_ADMIN", "")
    id_company = sv.get("ID_SOCIETE", "")
    if not m or not id_user:
        return False
    base = f"{urlsplit(dossier_url).scheme}://{urlsplit(dossier_url).netloc}"
    url = (f"{base}/Zenith/BackOffice/{m.group(1)}/BookingEngine/PollSession"
           f"?idUser={id_user}&idCompany={id_company}")
    try:
        r = session.session.get(url, timeout=30, allow_redirects=True,
                                headers={**_BROWSER_HEADERS, "Referer": dossier_url})
        ok = "PollSession Successful" in (r.text or "")
        log.info("PollSession %s -> %s", url, "OK" if ok else f"({r.status_code})")
        return ok
    except Exception as exc:  # noqa: BLE001
        log.info("PollSession failed: %s", exc)
        return False


def _get_traveler_update(session, url: str, referer: str, timeout_s: float) -> str:
    resp = session.session.get(
        url, timeout=timeout_s, allow_redirects=True,
        headers={**_BROWSER_HEADERS, "Referer": referer})
    return resp.text or ""


def fetch_passenger_details(
    session,
    dossier_html: str,
    dossier_url: str,
    *,
    pnr: str = "",
    timeout_s: float = 120.0,
    max_passengers: int = 50,
) -> list[PassengerDetail]:
    """Fetch ALL passengers' detail for one PNR from the modern BackOffice app.

    Two strategies, tried in order and then cached on the session so the rest of a
    bulk run uses only the one that works:
      A. DIRECT — GET the constructed Traveler/Update URL (1 request). If the modern
         session isn't bootstrapped yet, call PollSession once and retry.
      B. POSTBACK — replay the browser's full `__doPostBack`; the legacy server 302s
         to the same modern URL, which is followed automatically. Byte-faithful to
         the real UI, so it works even if a cold GET is refused.
    Fail-safe: any error returns []."""
    url = traveler_update_url(dossier_html, dossier_url)
    if not url:
        return []
    strategy = getattr(session, "_pax_strategy", None)
    try:
        # Once a bulk run has learned the winning path, take it straight away.
        if strategy == "postback":
            _ensure_backoffice_session(session, dossier_html, dossier_url)
            return _fetch_via_postback(session, dossier_html, dossier_url, pnr, timeout_s)[:max_passengers]

        # Strategy A — direct GET.
        text = _get_traveler_update(session, url, dossier_url, timeout_s)
        pax = parse_traveler_update_html(text, pnr=pnr)
        if pax:
            _remember_strategy(session, "direct")
            return pax[:max_passengers]

        if "Travelers[" not in text:
            # Bootstrap the modern session (once) and retry the direct GET.
            if _ensure_backoffice_session(session, dossier_html, dossier_url):
                text = _get_traveler_update(session, url, dossier_url, timeout_s)
                pax = parse_traveler_update_html(text, pnr=pnr)
                if pax:
                    log.info("%s: passenger detail via direct GET (after PollSession)", pnr or "PNR")
                    _remember_strategy(session, "direct")
                    return pax[:max_passengers]
            # Strategy B — browser-faithful postback -> 302 -> modern GET.
            if "Travelers[" not in text:
                pax = _fetch_via_postback(session, dossier_html, dossier_url, pnr, timeout_s)
                if pax:
                    log.info("%s: passenger detail via postback->302 fallback", pnr or "PNR")
                    _remember_strategy(session, "postback")
                    return pax[:max_passengers]
    except Exception as exc:  # noqa: BLE001 — never sink the PNR on a detail error
        log.info("passenger detail failed for %s: %s", pnr, exc)
        return []
    return pax[:max_passengers] if pax else []


def fetch_passenger_details_postback(
    session,
    dossier_html: str,
    dossier_url: str,
    *,
    pnr: str = "",
    timeout_s: float = 120.0,
    max_passengers: int = 20,
) -> list[PassengerDetail]:
    """LEGACY (dead on current Zenith): replay each passenger's __doPostBack. Kept
    for reference/tests; the platform moved passenger detail to the modern app."""
    ctx = extract_postback_context(dossier_html)
    if not ctx.action or not ctx.passenger_targets:
        return []
    post_url = urljoin(dossier_url, ctx.action)
    targets = ctx.passenger_targets
    if len(targets) > max_passengers:
        log.warning("%s: %d passengers, capping at %d", pnr or "PNR",
                    len(targets), max_passengers)
        targets = targets[:max_passengers]

    out: list[PassengerDetail] = []
    seen: set[tuple] = set()   # belt-and-suspenders identity dedup
    import time as _t
    for i, target in enumerate(targets, start=1):
        form = dict(ctx.hidden)
        form["__EVENTTARGET"] = target
        form["__EVENTARGUMENT"] = ""
        resp = None
        for attempt in range(3):   # retry transient 5xx / network on the postback
            try:
                resp = session.session.post(
                    post_url, data=form, timeout=timeout_s, allow_redirects=True)
            except Exception as exc:  # noqa: BLE001 — one bad pax must not sink the rest
                log.info("passenger postback %d attempt %d error: %s", i, attempt + 1, exc)
                resp = None
            if resp is not None and getattr(resp, "status_code", 200) < 500:
                break
            if attempt < 2:
                _t.sleep(1.5 * (attempt + 1))
        if resp is None:
            continue
        detail = parse_passenger_form(resp.text, pnr=pnr, index=len(out) + 1)
        if detail is None:
            continue
        key = (detail.last_name.upper(), detail.first_name.upper(),
               detail.date_of_birth, detail.document_number)
        if key in seen:
            continue
        seen.add(key)
        out.append(detail)
    return out


def diagnose_passenger_fetch(
    session, dossier_html: str, dossier_url: str, *, pnr: str = "",
    out_dir=None, timeout_s: float = 120.0,
) -> list[str]:
    """Explain WHY passenger extraction succeeds or returns nothing, and save the
    dossier + first postback response to `out_dir` for inspection. Returns human-
    readable log lines. Used to pinpoint the obstacle without a HAR."""
    lines: list[str] = []
    tag = f"[diag {pnr}]"
    ctx = extract_postback_context(dossier_html)
    named = _PAX_TARGET_NAME_RE.findall(dossier_html)
    raw_targets = _PAX_TARGET_RE.findall(dossier_html)
    lines.append(f"{tag} dossier={len(dossier_html)} chars, form action={'yes' if ctx.action else 'MISSING'}, "
                 f"hidden fields={len(ctx.hidden)}")
    lines.append(f"{tag} passenger links: named={len(named)} raw={len(raw_targets)} "
                 f"after-dedup={len(ctx.passenger_targets)}")

    def _dump(name: str, text: str) -> None:
        if not out_dir:
            return
        try:
            p = Path(out_dir) / f"_paxdiag_{(pnr or 'PNR')}_{name}.html"
            p.write_text(text or "", encoding="utf-8", errors="replace")
            lines.append(f"{tag} saved {p.name}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{tag} could not save {name}: {exc}")

    _dump("dossier", dossier_html)

    # The passenger detail lives in the modern BackOffice app now: one GET to
    # Traveler/Update?idPNR=<Id_Dossier> returns all travelers. Diagnose THAT.
    url = traveler_update_url(dossier_html, dossier_url)
    if not url:
        lines.append(f"{tag} -> no modern idPNR in the dossier — cannot build Traveler/Update URL. "
                     f"See saved dossier HTML.")
        return lines
    lines.append(f"{tag} Traveler/Update url = …{url[-90:]}")

    def _markers(text: str, final_url: str = "") -> list[str]:
        return [k for k in ("Travelers[", "DocumentNumber", "Email", "Nationalit",
                            "captcha-delivery", "ERROR PAGE", "/otds/")
                if k in text or k in final_url]

    def _report_ok(where: str, pax: list) -> list[str]:
        lines.append(f"{tag} PARSED OK via {where} -> {len(pax)} passenger(s); first="
                     f"{pax[0].first_name} {pax[0].last_name} doc={pax[0].document_number or '(none)'}")
        return lines

    # Strategy A — direct GET.
    try:
        r = session.session.get(url, timeout=timeout_s, allow_redirects=True,
                                headers={**_BROWSER_HEADERS, "Referer": dossier_url})
    except Exception as exc:  # noqa: BLE001
        lines.append(f"{tag} direct GET error: {type(exc).__name__}: {exc}")
        r = None
    if r is not None:
        text = r.text or ""
        lines.append(f"{tag} [A] direct GET -> status={r.status_code} len={len(text)} "
                     f"final=…{(r.url or '')[-45:]} markers={_markers(text, r.url or '') or '(none)'}")
        _dump("A_direct_get", text)
        pax = parse_traveler_update_html(text, pnr=pnr)
        if pax:
            return _report_ok("direct GET", pax)

    # Strategy A+ — bootstrap the modern session (PollSession), retry the GET.
    poll_ok = establish_backoffice_session(session, dossier_html, dossier_url)
    lines.append(f"{tag} [A+] PollSession bootstrap -> {'OK' if poll_ok else 'FAILED'} "
                 f"(idUser={((getattr(session, 'state_values', None) or {}).get('ID_ADMIN')) or '?'})")
    if poll_ok:
        try:
            r = session.session.get(url, timeout=timeout_s, allow_redirects=True,
                                    headers={**_BROWSER_HEADERS, "Referer": dossier_url})
            text = r.text or ""
            lines.append(f"{tag} [A+] retry GET -> status={r.status_code} len={len(text)} "
                         f"markers={_markers(text, r.url or '') or '(none)'}")
            _dump("Aplus_retry_get", text)
            pax = parse_traveler_update_html(text, pnr=pnr)
            if pax:
                return _report_ok("direct GET after PollSession", pax)
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{tag} [A+] retry GET error: {type(exc).__name__}: {exc}")

    # Strategy B — browser-faithful postback -> 302 -> modern GET.
    ctx = extract_postback_context(dossier_html)
    if ctx.action and ctx.passenger_targets:
        try:
            body = build_passenger_postback_body(dossier_html, ctx.passenger_targets[0])
            post_url = urljoin(dossier_url, ctx.action)
            r = session.session.post(post_url, data=body, timeout=timeout_s, allow_redirects=True,
                                     headers={**_BROWSER_HEADERS, "Referer": dossier_url,
                                              "Content-Type": "application/x-www-form-urlencoded"})
            text = r.text or ""
            lines.append(f"{tag} [B] postback ({len(body)} fields) -> status={r.status_code} "
                         f"len={len(text)} final=…{(r.url or '')[-45:]} "
                         f"markers={_markers(text, r.url or '') or '(none)'}")
            _dump("B_postback", text)
            pax = parse_traveler_update_html(text, pnr=pnr)
            if pax:
                return _report_ok("postback->302 fallback", pax)
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{tag} [B] postback error: {type(exc).__name__}: {exc}")
    else:
        lines.append(f"{tag} [B] skipped — no form action / passenger targets in dossier")

    lines.append(f"{tag} 0 parsed by ALL strategies. 'captcha-delivery'=bot-blocked; "
                 f"'ERROR PAGE'/'otds'=session not accepted. See _paxdiag_{pnr}_*.html.")
    return lines
