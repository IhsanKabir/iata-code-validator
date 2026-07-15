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
            title=_trav_select(html, idx, "Civility"),
            gender=_trav_select(html, idx, "Gender"),
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


def fetch_passenger_details(
    session,
    dossier_html: str,
    dossier_url: str,
    *,
    pnr: str = "",
    timeout_s: float = 120.0,
    max_passengers: int = 50,
) -> list[PassengerDetail]:
    """Fetch ALL passengers' detail for one PNR via the modern Traveler/Update GET
    (one request per PNR). Fail-safe: any error returns [] rather than raising."""
    url = traveler_update_url(dossier_html, dossier_url)
    if not url:
        return []
    try:
        resp = session.session.get(
            url, timeout=timeout_s, allow_redirects=True,
            headers={
                # Match the browser so the BackOffice app (and any bot filter)
                # treats it like the real UI navigating from the dossier.
                "Referer": dossier_url,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/126.0.0.0 Safari/537.36"),
            })
    except Exception as exc:  # noqa: BLE001 — never sink the PNR on a detail error
        log.info("Traveler/Update GET failed for %s: %s", pnr, exc)
        return []
    return parse_traveler_update_html(resp.text or "", pnr=pnr)[:max_passengers]


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
    try:
        r = session.session.get(
            url, timeout=timeout_s, allow_redirects=True,
            headers={"Referer": dossier_url,
                     "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/126.0.0.0 Safari/537.36")})
    except Exception as exc:  # noqa: BLE001
        lines.append(f"{tag} Traveler/Update GET error: {type(exc).__name__}: {exc}")
        return lines
    text = r.text or ""
    marks = [k for k in ("Travelers[", "DocumentNumber", "Email", "Nationalit",
                         "captcha-delivery", "ERROR PAGE", "/otds/")
             if k in text or k in (r.url or "")]
    lines.append(f"{tag} Traveler/Update -> status={r.status_code} len={len(text)} "
                 f"final=…{(r.url or '')[-45:]} markers={marks or '(none)'}")
    _dump("traveler_update", text)
    pax = parse_traveler_update_html(text, pnr=pnr)
    if pax:
        lines.append(f"{tag} PARSED OK -> {len(pax)} passenger(s); first="
                     f"{pax[0].first_name} {pax[0].last_name} doc={pax[0].document_number or '(none)'}")
    else:
        lines.append(f"{tag} 0 parsed. If markers show 'captcha-delivery' the app is bot-blocked; "
                     f"'ERROR PAGE'/'otds' = session not accepted. See _paxdiag_{pnr}_traveler_update.html.")
    return lines
