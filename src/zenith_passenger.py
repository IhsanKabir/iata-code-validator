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


def fetch_passenger_details(
    session,
    dossier_html: str,
    dossier_url: str,
    *,
    pnr: str = "",
    timeout_s: float = 120.0,
    max_passengers: int = 20,
) -> list[PassengerDetail]:
    """Replay each passenger's __doPostBack and parse the returned detail form.

    `session` is a ZenithSession (uses `.session`). Fail-safe: a passenger whose
    postback errors or returns a non-form is skipped, not fatal. Returns one
    PassengerDetail per passenger the postback resolved."""
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
    if not ctx.passenger_targets:
        lines.append(f"{tag} -> NO passenger links found. On this booking type the passenger "
                     f"name is not a __doPostBack('...linkPassager') link. See saved dossier HTML.")
        return lines

    post_url = urljoin(dossier_url, ctx.action)
    lines.append(f"{tag} POST url = {post_url[:90]}")
    form = dict(ctx.hidden)
    form["__EVENTTARGET"] = ctx.passenger_targets[0]
    form["__EVENTARGUMENT"] = ""
    try:
        resp = session.session.post(post_url, data=form, timeout=timeout_s, allow_redirects=True)
    except Exception as exc:  # noqa: BLE001
        lines.append(f"{tag} postback POST error: {type(exc).__name__}: {exc}")
        return lines
    text = resp.text or ""
    lines.append(f"{tag} postback -> status={resp.status_code} len={len(text)} "
                 f"final_url=…{resp.url[-55:]}")
    marks = {
        "txtFirstName": "txtFirstName" in text, "txtLastName": "txtLastName" in text,
        "txtNom": "txtNom" in text, "txtPrenom": "txtPrenom" in text,
        "txtEmail": "txtEmail" in text, "linkPassager": "linkPassager" in text,
        "_lblPNRCode(dossier)": "_lblPNRCode" in text, "login/otds": "/otds/" in (resp.url or ""),
        "invalid-postback": ("Invalid postback" in text or "potentially dangerous" in text.lower()),
        "viewstate-MAC-err": ("Validation of viewstate" in text or "viewstate MAC" in text.lower()),
    }
    present = ", ".join(k for k, v in marks.items() if v) or "(none of the expected markers)"
    lines.append(f"{tag} response markers: {present}")
    _dump("postback", text)
    parsed = parse_passenger_form(text, pnr=pnr, index=1)
    if parsed:
        lines.append(f"{tag} PARSED OK -> {parsed.first_name} {parsed.last_name} "
                     f"doc={parsed.document_number or '(none)'}")
    else:
        lines.append(f"{tag} parse returned None: the postback response is NOT the passenger "
                     f"form. Read the saved _paxdiag_{pnr}_postback.html to see what came back.")

    # The passenger detail migrated to the modern BackOffice MVC app. Probe the
    # Traveler endpoints the dossier links to (they carry idPNR) and dump them.
    from urllib.parse import urlsplit
    base = f"{urlsplit(dossier_url).scheme}://{urlsplit(dossier_url).netloc}"
    modern = re.findall(r"/Zenith/[A-Za-z]+/[^\"'<> ]*?Traveler[^\"'<> ]*", dossier_html)
    modern = list(dict.fromkeys(unescape(u) for u in modern))
    lines.append(f"{tag} modern Traveler URLs in dossier: {len(modern)}")
    for j, rel in enumerate(modern[:3], start=1):
        url = base + rel
        try:
            r = session.session.get(url, timeout=timeout_s, allow_redirects=True)
            t = r.text or ""
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{tag} modern[{j}] GET error: {exc}")
            continue
        hits = [k for k in ("txtFirstName", "txtLastName", "Passport", "passport",
                            "DocumentNumber", "Nationalit", "Email", "idTraveler",
                            "Traveler/Update", "input", "Surname", "GivenName")
                if k in t]
        lines.append(f"{tag} modern[{j}] {rel[:60]} -> status={r.status_code} len={len(t)} "
                     f"markers={hits or '(none)'}")
        _dump(f"modern{j}", t)
    return lines
