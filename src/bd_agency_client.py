"""HTTP client for the Bangladesh Travel Agency Management System.

Endpoint: https://regtravelagency.gov.bd/get-list
  - Returns ~6,113 currently approved (active) travel agencies in JSON.
  - Requires a CSRF token scraped from the homepage HTML and a session
    cookie from the same homepage GET.

This module exposes:

  fetch_all_agencies(session=None)  -> list[Agency]
      Fetches the full list in one round-trip (DataTables length=-1).

  parse_agency_record(raw)          -> Agency
      Splits the HTML-blob fields into clean Name / License / Email /
      Mobile / Website / Address.

The site only exposes ACTIVE agencies. Each record has a
`license_expired_date` — we expose this as `Status` (ACTIVE if expiry
in the future, EXPIRED-PENDING otherwise).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable

import requests

log = logging.getLogger(__name__)

HOMEPAGE_URL = "https://regtravelagency.gov.bd/"
LIST_URL = "https://regtravelagency.gov.bd/get-list"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Agency:
    agency_name: str
    license_no: str
    email: str
    mobile: str
    website: str
    address: str
    license_expired_date: str  # ISO yyyy-mm-dd, may be empty
    status: str                # ACTIVE | EXPIRED-PENDING
    raw_id: int = 0

    def matches_token(self, token: str) -> bool:
        """Used by simple ad-hoc filtering."""
        t = token.lower().strip()
        return (
            t in self.agency_name.lower()
            or t in self.license_no.lower()
        )


# ---------------------------------------------------------------------------
# CSRF + session bootstrap
# ---------------------------------------------------------------------------


_CSRF_RE = re.compile(
    r'name="csrf-token"\s+content="([^"]+)"',
    re.IGNORECASE,
)


def _bootstrap_session() -> tuple[requests.Session, str]:
    """Create a session, GET the homepage, return session + csrf token."""
    sess = requests.Session()
    sess.headers["User-Agent"] = USER_AGENT
    sess.headers["Accept-Language"] = "en-US,en;q=0.9"
    resp = sess.get(HOMEPAGE_URL, timeout=30)
    resp.raise_for_status()
    match = _CSRF_RE.search(resp.text)
    if match is None:
        raise RuntimeError("Could not find csrf-token meta tag on homepage")
    return sess, match.group(1)


# ---------------------------------------------------------------------------
# Field parsing
# ---------------------------------------------------------------------------


_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
# Email = anything looking like x@y.z
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# BD mobile numbers: 11 digits starting with 01, possibly with +88 prefix
_MOBILE_RE = re.compile(r"(?:\+?88)?0?1[3-9]\d{8}")
# Website: starts with http or has www. or ends with common TLDs
_URL_RE = re.compile(
    r"(?:https?://[^\s,;]+|www\.[^\s,;]+|[A-Za-z0-9-]+\.(?:com|net|org|info|biz|io|co|bd|gov)[^\s,;]*)",
    re.IGNORECASE,
)


def _split_lines(html_blob: str) -> list[str]:
    """Split a `<br>`-separated HTML blob into clean trimmed text lines."""
    if not html_blob:
        return []
    parts = _BR_RE.split(html_blob)
    out: list[str] = []
    for p in parts:
        # Strip any other tags then trim
        text = _TAG_RE.sub("", p).strip()
        # Collapse internal whitespace
        text = re.sub(r"\s+", " ", text)
        if text:
            out.append(text)
    return out


def _split_name_license(blob: str) -> tuple[str, str]:
    """`agency_name_license` looks like `NAME<br>LICENSE`."""
    parts = _split_lines(blob)
    if len(parts) >= 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def _split_contact(blob: str) -> tuple[str, str, str]:
    """`agency_email_number_website` may contain email, mobile, website
    (in any order, with `<br>` separators). Returns (email, mobile, website)."""
    parts = _split_lines(blob)
    email, mobile, website = "", "", ""
    for p in parts:
        if not email:
            m = _EMAIL_RE.search(p)
            if m:
                email = m.group(0)
                continue
        if not mobile:
            m = _MOBILE_RE.search(p)
            if m:
                mobile = m.group(0)
                continue
        if not website:
            m = _URL_RE.search(p)
            if m:
                website = m.group(0)
                continue
    return email, mobile, website


def _clean_address(blob: str) -> str:
    parts = _split_lines(blob)
    return ", ".join(parts)


def _classify_status(expiry_iso: str) -> str:
    """ACTIVE if expiry >= today, EXPIRED-PENDING otherwise.

    Empty / unparseable dates are reported as ACTIVE (we have no info to
    contradict the site's "approved" flag).
    """
    if not expiry_iso:
        return "ACTIVE"
    try:
        expiry = datetime.strptime(expiry_iso[:10], "%Y-%m-%d").date()
    except ValueError:
        return "ACTIVE"
    return "ACTIVE" if expiry >= date.today() else "EXPIRED-PENDING"


def parse_agency_record(raw: dict) -> Agency:
    """Translate one `/get-list` JSON record into an `Agency`."""
    name, license_no = _split_name_license(raw.get("agency_name_license", ""))
    email, mobile, website = _split_contact(
        raw.get("agency_email_number_website", "")
    )
    address = _clean_address(raw.get("business_address_en", ""))
    expiry = (raw.get("license_expired_date") or "")[:10]
    return Agency(
        agency_name=name,
        license_no=license_no,
        email=email,
        mobile=mobile,
        website=website,
        address=address,
        license_expired_date=expiry,
        status=_classify_status(expiry),
        raw_id=int(raw.get("id") or 0),
    )


# ---------------------------------------------------------------------------
# Public fetch API
# ---------------------------------------------------------------------------


def fetch_all_agencies(
    timeout_s: float = 60.0,
    session: requests.Session | None = None,
) -> list[Agency]:
    """Single-shot fetch of every active agency.

    DataTables endpoint accepts `length=-1` to mean "all rows". The server
    happily returns ~6,113 rows in one ~1-2 MB JSON response.
    """
    if session is None:
        sess, csrf = _bootstrap_session()
    else:
        sess = session
        # Re-bootstrap CSRF if the caller passed an existing session
        resp = sess.get(HOMEPAGE_URL, timeout=30)
        resp.raise_for_status()
        m = _CSRF_RE.search(resp.text)
        if m is None:
            raise RuntimeError("Could not find csrf-token on homepage")
        csrf = m.group(1)

    headers = {
        "X-CSRF-TOKEN": csrf,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Origin": "https://regtravelagency.gov.bd",
        "Referer": HOMEPAGE_URL,
    }
    payload = {
        "draw": 1,
        "start": 0,
        "length": -1,             # all
        "search[value]": "",
        "search[regex]": "false",
    }

    log.info("Fetching all agencies from %s", LIST_URL)
    resp = sess.post(LIST_URL, headers=headers, data=payload, timeout=timeout_s)
    resp.raise_for_status()
    body = resp.json()
    rows = body.get("data") or []
    log.info(
        "Got %d records (recordsTotal=%s, recordsFiltered=%s)",
        len(rows),
        body.get("recordsTotal"),
        body.get("recordsFiltered"),
    )

    agencies: list[Agency] = []
    for raw in rows:
        try:
            agencies.append(parse_agency_record(raw))
        except Exception as e:  # noqa: BLE001 — never let one bad row kill the batch
            log.warning("Skipping malformed record %r: %s", raw.get("id"), e)
    return agencies


# ---------------------------------------------------------------------------
# Helpers for callers
# ---------------------------------------------------------------------------


def filter_status(agencies: Iterable[Agency], include_expired_pending: bool) -> list[Agency]:
    if include_expired_pending:
        return list(agencies)
    return [a for a in agencies if a.status == "ACTIVE"]
