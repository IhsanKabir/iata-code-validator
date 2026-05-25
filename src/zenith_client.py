"""HTTP client for Zenith TT Interactive (asia.ttinteractive.com).

Bulk extracts customer records (name, email, phone, address, etc.) given
a list of Customer IDs. Read-only — never POSTs to update/create.

Flow (one-time, per session):

  1. POST  /otds/index.asp?action=TESTLOGIN           credentials
  2. Follow 4-5 redirects → /NewUI/aerien/f_index.asp
  3. Parse `var stateValues = {...}` for ID_ADMIN, ID_SOCIETE, ID_APPLICATION
  4. GET   /TTIDotNet/.../InitUserContext.aspx?...    initializes ASPX session

Flow (per customer):

  5. GET   /TTIDOTNET/.../FinalCustomer.ashx?IdCustomer={id}    302 →
  6. GET   /TTIDOTNET/.../Customer.aspx?taskId={new_uuid}&...   ~150 KB HTML

The HTML is parsed by regex — ASP.NET WebForms field names like
`mUsrMain$UsrFinalCustomer$UsrFinalCustomer_MainInformation1$txtLastName`
are stable enough across customers to anchor extraction.

Concurrency model: `fetch_many` uses a ThreadPoolExecutor with N workers,
each making serial calls with a configurable inter-call delay. Per-worker
delay × N workers = effective rate. Tests at N=1 first, then dial up.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Iterable

import requests

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://asia.ttinteractive.com"
LOGIN_URL = f"{BASE_URL}/otds/index.asp?action=TESTLOGIN"
LANDING_URL = f"{BASE_URL}/NewUI/aerien/f_index.asp"
INIT_CONTEXT_URL = (
    f"{BASE_URL}/TTIDotNet/Transport/TransportNetBO2/Sales/InitUserContext.aspx"
)
CUSTOMER_LOOKUP_URL = (
    f"{BASE_URL}/TTIDOTNET/TRANSPORT/TRANSPORTNETBO2/SALES2/CustomerViews/FinalCustomer.ashx"
)
FLIGHT_LOAD_URL = f"{BASE_URL}/newui/aerien/commercial/Sale_ListeVols_NewStock.asp"

# Server-side limit: max 10 pages of results per single search.
# Combined with NbReponse=100, that's 1000 records per search — so we
# chunk the user's date range to keep each search under that cap.
FLIGHT_LOAD_MAX_PAGES = 10
FLIGHT_LOAD_DEFAULT_PAGE_SIZE = 100

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# Lookup status codes recorded in the cache.
STATUS_OK = "OK"
STATUS_NOT_FOUND = "NOT_FOUND"
STATUS_ERROR = "ERROR"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ZenithError(Exception):
    """Base class — any failure talking to Zenith."""


class LoginError(ZenithError):
    """Credentials rejected, or company code wrong."""


class SessionExpiredError(ZenithError):
    """A previously-good session is no longer valid. Caller should re-login."""


class CustomerNotFoundError(ZenithError):
    """Zenith found no customer with the given ID."""


class RateLimitedError(ZenithError):
    """Zenith returned 429 / 503 / a known rate-limit signal."""


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CustomerRecord:
    """One parsed customer page.

    The schema mirrors the on-screen "Edit Final Customer" form so users
    recognize the columns immediately. All fields are strings (no
    parsing/normalization) — Zenith's own format is what hits the Excel.
    """

    customer_id: str
    title: str = ""
    first_name: str = ""
    middle_name: str = ""
    last_name: str = ""
    date_of_birth: str = ""
    email: str = ""
    home_phone: str = ""
    home_phone_international: str = ""
    mobile_phone: str = ""
    mobile_phone_international: str = ""
    office_phone: str = ""
    nationality: str = ""
    language: str = ""
    spoken_language: str = ""
    address: str = ""
    city: str = ""
    postal_code: str = ""
    country: str = ""
    registration_date: str = ""


@dataclass(frozen=True)
class LookupResult:
    """Outcome of one fetch attempt — success, not-found, or error."""

    customer_id: str
    status: str  # STATUS_OK / STATUS_NOT_FOUND / STATUS_ERROR
    record: CustomerRecord | None = None
    error: str = ""
    checked_at: str = ""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

# ASP.NET WebForms prefixes every input with a long control hierarchy;
# we anchor on the leaf `txtFieldName` / `ddlFieldName` only.
_TEXT_FIELD_RE = re.compile(
    r'name="[^"]*\$(txt[A-Za-z]+)"[^>]*?\bvalue="([^"]*)"',
    re.IGNORECASE,
)
_SELECT_BLOCK_RE = re.compile(
    r'<select[^>]+name="[^"]*\$(ddl[A-Za-z]+)"[^>]*>(.*?)</select>',
    re.IGNORECASE | re.DOTALL,
)
_SELECTED_OPTION_RE = re.compile(
    r'<option[^>]+selected[^>]*>([^<]+)</option>',
    re.IGNORECASE,
)
_REG_DATE_RE = re.compile(
    r'id="[^"]*_lblRegistrationDate"[^>]*>([^<]+)</span>',
    re.IGNORECASE,
)

# A NOT_FOUND page typically lacks any txtFirstName / txtLastName field.
# We treat "no first AND no last name AND no email" as missing.
_MARKERS_OF_PRESENCE = ("txtFirstName", "txtLastName", "txtEmail")


def parse_customer_html(html: str, customer_id: str) -> CustomerRecord:
    """Extract a CustomerRecord from the Customer.aspx HTML.

    Raises CustomerNotFoundError if the page doesn't look like a real
    customer page (e.g. Zenith silently redirects to an empty form when
    the ID doesn't exist).
    """
    if not any(marker in html for marker in _MARKERS_OF_PRESENCE):
        raise CustomerNotFoundError(f"No customer fields in page for ID {customer_id}")

    text_fields = dict(_TEXT_FIELD_RE.findall(html))
    select_fields: dict[str, str] = {}
    for name, block in _SELECT_BLOCK_RE.findall(html):
        m = _SELECTED_OPTION_RE.search(block)
        if m:
            select_fields[name] = m.group(1).strip()

    reg_date_match = _REG_DATE_RE.search(html)
    registration_date = reg_date_match.group(1).strip() if reg_date_match else ""

    def t(name: str) -> str:
        return text_fields.get(name, "").strip()

    def s(name: str) -> str:
        v = select_fields.get(name, "").strip()
        # Zenith uses "Select..." for empty dropdowns — treat as blank.
        return "" if v in ("Select...", "Select…") else v

    # `txtCountry` lives on the address card and is sometimes a text field;
    # the dropdown form uses ddlCountry. Prefer the dropdown when set.
    country = s("ddlCountry") or t("txtCountry")

    record = CustomerRecord(
        customer_id=customer_id,
        title=s("ddlTitle"),
        first_name=t("txtFirstName"),
        middle_name=t("txtMiddleName"),
        last_name=t("txtLastName"),
        date_of_birth=t("txtDateOfBirth"),
        email=t("txtEmail"),
        home_phone=t("txtHomePhoneNumber"),
        home_phone_international=t("txtHomePhoneNumberInternational"),
        mobile_phone=t("txtMobilePhoneNumber"),
        mobile_phone_international=t("txtMobilePhoneNumberInternational"),
        office_phone=t("txtOfficePhoneNumber"),
        nationality=s("ddlNationality"),
        language=s("ddlLanguage"),
        spoken_language=s("ddlSpokenLanguage"),
        address=t("txtAddress"),
        city=t("txtCity"),
        postal_code=t("txtPostalCode"),
        country=country,
        registration_date=registration_date,
    )
    return record


_STATE_VALUES_RE = re.compile(
    r'var\s+stateValues\s*=\s*\{(.*?)\}\s*;',
    re.DOTALL,
)
_STATE_KV_RE = re.compile(r'"([^"]+)"\s*:\s*"([^"]*)"')


def parse_state_values(landing_html: str) -> dict[str, str]:
    """Extract the `var stateValues = { ... }` block from f_index.asp."""
    m = _STATE_VALUES_RE.search(landing_html)
    if not m:
        raise LoginError(
            "Landing page didn't contain stateValues — login may have failed "
            "or the page layout changed."
        )
    return dict(_STATE_KV_RE.findall(m.group(1)))


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@dataclass
class ZenithSession:
    """One logged-in Zenith session.

    Use `from_credentials` to log in; call `fetch_customer` to look up
    individual IDs. The session is thread-safe for read-only customer
    lookups because requests.Session locks per-host connection pools —
    but each fetch creates its own request, no shared mutable state.
    """

    session: requests.Session
    state_values: dict[str, str] = field(default_factory=dict)
    company_code: str = "usba"

    @classmethod
    def from_credentials(
        cls,
        username: str,
        password: str,
        *,
        company_code: str = "usba",
        timeout_s: float = 30.0,
    ) -> "ZenithSession":
        """Run the full login + init-context handshake."""
        sess = requests.Session()
        sess.headers["User-Agent"] = USER_AGENT

        # Step 1: TESTLOGIN POST. Allow redirects — Zenith chains 4-5 of them.
        log.info("Zenith login as %r (company=%s)", username, company_code)
        try:
            resp = sess.post(
                LOGIN_URL,
                data={
                    "Secured": "1",
                    "login": username,
                    "pwd": password,
                    "LoginCompanyIdentificationCode": company_code,
                    "imageFond": "/otds/images/aeropack.jpg",
                    "smartclient": "",
                    "extranetagencyURL": "",
                    "RadioApplication": "Aeropack",
                },
                allow_redirects=True,
                timeout=timeout_s,
            )
        except requests.RequestException as exc:
            raise LoginError(f"Network error during login: {exc}") from exc

        # If the final page is still on /otds/, the login failed.
        if "/otds/" in resp.url and "f_index" not in resp.url:
            raise LoginError(
                f"Login rejected by Zenith — landed at {resp.url} instead of "
                "the dashboard. Check username, password, and company code."
            )

        # Step 2: Make sure we have the landing page (which carries stateValues).
        if "f_index" not in resp.url:
            log.info("Following extra redirect to landing page")
            resp = sess.get(LANDING_URL, timeout=timeout_s)
            resp.raise_for_status()

        state_values = parse_state_values(resp.text)
        log.info(
            "Zenith state values: user=%s company=%s app=%s",
            state_values.get("ID_ADMIN"),
            state_values.get("ID_SOCIETE"),
            state_values.get("ID_APPLICATION"),
        )

        # Step 3: InitUserContext — primes the .NET BO session.
        init_resp = sess.get(
            INIT_CONTEXT_URL,
            params={
                "Id_Utilisateur": state_values.get("ID_ADMIN", ""),
                "Id_Societe": state_values.get("ID_SOCIETE", ""),
                "Id_Application": state_values.get("ID_APPLICATION", "3"),
                "Id_Langue": state_values.get("ID_LANGUE", "2"),
            },
            timeout=timeout_s,
        )
        init_resp.raise_for_status()

        return cls(session=sess, state_values=state_values, company_code=company_code)

    def fetch_customer(
        self, customer_id: str, *, timeout_s: float = 45.0,
    ) -> CustomerRecord:
        """Fetch and parse one customer record.

        Raises CustomerNotFoundError, SessionExpiredError, RateLimitedError,
        or ZenithError on failures.
        """
        params = {"IdCustomer": str(customer_id).strip()}
        try:
            resp = self.session.get(
                CUSTOMER_LOOKUP_URL,
                params=params,
                allow_redirects=True,
                timeout=timeout_s,
            )
        except requests.RequestException as exc:
            raise ZenithError(f"Network error fetching {customer_id}: {exc}") from exc

        if resp.status_code in (401, 403):
            raise SessionExpiredError(
                f"Zenith returned {resp.status_code} for ID {customer_id} — "
                "session expired or never authenticated."
            )
        if resp.status_code in (429, 503):
            raise RateLimitedError(
                f"Zenith returned {resp.status_code} for ID {customer_id} — "
                "back off and retry."
            )
        if resp.status_code >= 500:
            raise ZenithError(
                f"Zenith returned {resp.status_code} for ID {customer_id}."
            )
        # 200 means we got the customer page (or a not-found surrogate).
        if "/otds/" in resp.url:
            raise SessionExpiredError(
                f"Zenith redirected ID {customer_id} back to login — session lost."
            )
        return parse_customer_html(resp.text, str(customer_id))


# ---------------------------------------------------------------------------
# Concurrent bulk fetcher
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def fetch_many(
    session: ZenithSession,
    customer_ids: Iterable[str],
    *,
    concurrency: int = 3,
    delay_s: float = 0.8,
    progress_cb: Callable[[LookupResult, int, int], None] | None = None,
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
    on_session_expired: Callable[[], bool] | None = None,
) -> list[LookupResult]:
    """Look up many customer IDs concurrently.

    Args:
      session: a logged-in ZenithSession.
      customer_ids: iterable of strings.
      concurrency: number of worker threads (1..10 is the sane range).
      delay_s: per-worker delay between calls — politeness to Zenith.
      progress_cb: called after each result with (result, completed, total).
      stop_event: setting this causes new work to stop and the call to return.
      pause_event: when set, workers block in a tight loop until it clears.
      on_session_expired: called when a SessionExpiredError is caught.
                          Returns True to retry after caller re-logs in;
                          False to abort.

    Returns:
      LookupResult list in completion order — caller is expected to keep
      a reference to the input list if they need positional order.
    """
    ids = [str(i).strip() for i in customer_ids if str(i).strip()]
    results: list[LookupResult] = []
    total = len(ids)
    if total == 0:
        return results

    concurrency = max(1, min(int(concurrency), 10))
    delay_s = max(0.0, float(delay_s))

    def _worker(cid: str) -> LookupResult:
        # Cooperative pause
        if pause_event is not None:
            while pause_event.is_set():
                if stop_event is not None and stop_event.is_set():
                    return LookupResult(
                        customer_id=cid, status=STATUS_ERROR,
                        error="cancelled", checked_at=_now_iso(),
                    )
                time.sleep(0.2)
        if stop_event is not None and stop_event.is_set():
            return LookupResult(
                customer_id=cid, status=STATUS_ERROR,
                error="cancelled", checked_at=_now_iso(),
            )
        try:
            record = session.fetch_customer(cid)
            result = LookupResult(
                customer_id=cid, status=STATUS_OK, record=record,
                checked_at=_now_iso(),
            )
        except CustomerNotFoundError as exc:
            result = LookupResult(
                customer_id=cid, status=STATUS_NOT_FOUND,
                error=str(exc), checked_at=_now_iso(),
            )
        except SessionExpiredError as exc:
            # Surface to caller and stop.
            log.warning("Session expired during fetch of %s", cid)
            if on_session_expired is not None:
                handled = on_session_expired()
                if not handled and stop_event is not None:
                    stop_event.set()
            elif stop_event is not None:
                stop_event.set()
            result = LookupResult(
                customer_id=cid, status=STATUS_ERROR,
                error=f"SessionExpired: {exc}", checked_at=_now_iso(),
            )
        except RateLimitedError as exc:
            # Sleep longer than the delay then continue.
            log.warning("Rate limited on %s — sleeping 10s", cid)
            time.sleep(10.0)
            result = LookupResult(
                customer_id=cid, status=STATUS_ERROR,
                error=f"RateLimited: {exc}", checked_at=_now_iso(),
            )
        except Exception as exc:  # noqa: BLE001 — surface any leak
            log.exception("Unexpected error fetching %s", cid)
            result = LookupResult(
                customer_id=cid, status=STATUS_ERROR,
                error=f"{type(exc).__name__}: {exc}", checked_at=_now_iso(),
            )
        # Per-worker polite delay AFTER each call.
        if delay_s > 0:
            time.sleep(delay_s)
        return result

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_worker, cid): cid for cid in ids}
        completed = 0
        for fut in as_completed(futures):
            completed += 1
            r = fut.result()
            results.append(r)
            if progress_cb is not None:
                try:
                    progress_cb(r, completed, total)
                except Exception:  # noqa: BLE001 — never let callback kill the run
                    log.exception("progress callback raised")
            if stop_event is not None and stop_event.is_set():
                # Drain any remaining futures as cancelled
                for remaining in futures:
                    if not remaining.done():
                        remaining.cancel()
                break
    return results


# ---------------------------------------------------------------------------
# Flight Load (PNL) — date-range bulk pull
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlightLoadRow:
    """One leg row from the View PNLs report.

    The output Excel writes one row per (flight, leg, cabin). Numeric
    fields stay as strings here — we don't try to parse "152(0)" into
    components; the consumer can split if needed. The Excel exporter
    does the splitting for convenience.
    """

    flight_number: str
    day_of_week: str
    flight_date: str          # DD/MM/YYYY
    departure_time: str       # HH:MM (flight start, from header)
    aircraft: str             # "Boeing 737-800" — type only
    registration: str         # "S2-AJE" — tail number
    total_tickets_issued: str

    leg_id_vol: str           # leg-level id (per route segment)
    leg_route: str            # "SHJ-DAC"
    leg_origin: str           # "SHJ"
    leg_destination: str      # "DAC"
    leg_local_time_range: str # "24/05/2026 01:30 - 08:05"
    leg_cabin: str            # "Economy"

    tickets_issued: str       # "152(0)" — issued + (0 hidden)
    tickets_wl: str           # "0(0)"
    seats_confirmed: str      # "[152]"
    seats_options: str        # "0(0)"
    seats_wl: str             # "[0]"
    seats_available: str      # "13/410 97%"

    inventory_status: str     # "AS-Flight open" / "Closed the 22/05/2026"
    comments: str = ""


# ---- regexes — anchored on stable structural markers ----

_FLIGHT_HEADER_RE = re.compile(
    # `BS308` is sometimes rendered as `BS<spaces>308` in the legacy markup,
    # so we tolerate whitespace between the airline code and the number.
    r'<b>(?P<airline>BS)\s*(?P<number>\d+)\s*-\s*'
    r'<font[^>]+>(?P<dow>[A-Z][a-z]{2})\s+(?P<date>\d{2}/\d{2}/\d{4})'
    r'&nbsp;(?P<time>\d{2}:\d{2})'
    r'<font[^>]+>&nbsp;(?P<aircraft>[^<]+?)</font></b>'
    r'\s*-\s*<b>(?P<tickets>\d+) Tickets issued</b>'
)

# A `<table ... data-table-leg ... data-id-vol="N">` opens a leg sub-block.
_LEG_TABLE_RE = re.compile(
    r'<table[^>]*data-table-leg[^>]*data-id-vol\s*=\s*"(?P<id>\d+)"[^>]*>'
    r'(?P<inner>.*?)</table>',
    re.DOTALL,
)
_BILLETS_TABLE_RE = re.compile(
    r'<table[^>]*data-table-billets[^>]*data-id-vol\s*=\s*"(?P<id>\d+)"[^>]*>'
    r'(?P<inner>.*?)</table>',
    re.DOTALL,
)
_SEATS_TABLE_RE = re.compile(
    r'<table[^>]*data-table-zs[^>]*data-id-vol\s*=\s*"(?P<id>\d+)"[^>]*>'
    r'(?P<inner>.*?)</table>',
    re.DOTALL,
)
# Inventory cell sits inside a table that carries the same data-id-vol.
_INVENTORY_RE = re.compile(
    r'<table[^>]*data-id-vol\s*=\s*"(?P<id>\d+)"[^>]*>'
    r'.{0,1500}?inventorystatus[^>]*title\s*=\s*(?P<title>[^>]+?)\s*>',
    re.DOTALL,
)

_ROUTE_RE = re.compile(r"<u>([A-Z]{3})-([A-Z]{3})</u>")
_LOCAL_TIME_RE = re.compile(
    r'<font color="blue">(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}\s*-\s*\d{2}:\d{2})</font>'
)
_CABIN_RE = re.compile(r"Cabin.*?<nobr>([^<]+?)</nobr>", re.DOTALL)
_FNT_TEXT_RE = re.compile(r'<font class="FNTListRow">\s*([^<]+?)\s*</font>')


_REGISTRATION_RE = re.compile(r"\bS2-[A-Z]{2,4}\b")


def _split_aircraft(text: str) -> tuple[str, str]:
    """Split "Boeing 737-800 - S2-AJE" → ("Boeing 737-800", "S2-AJE").

    Registration is anchored on the `S2-` prefix (US-Bangla tail
    numbers), so this handles "ATR 72 - 600 - S2-AKJ" correctly too.
    """
    text = re.sub(r"\s+", " ", text).strip()
    m = _REGISTRATION_RE.search(text)
    if not m:
        return text, ""
    registration = m.group(0)
    aircraft = text[: m.start()].rstrip(" -")
    return aircraft, registration


# Each metric cell carries a stable CSS class on its <td>. We anchor on
# the class and grab whatever sits inside the rightmost FNTListRow font.
# Tags are unclosed in this legacy markup, so we don't try to balance.
def _extract_cell(html: str, class_name: str) -> str:
    """Pull the value text out of `<td class="TDListRow {class_name}">...</td>`.

    Tolerant to unclosed font/div tags — strips tags conservatively from
    whatever text follows the FNTListRow anchor up to the next `<td` or
    `</tr>`.
    """
    # Allow end-of-string as a cell boundary too — the per-stop TR
    # captures used in multi-stop flights don't include the closing
    # `</tr>`, so the LAST cell in a row had no terminator before.
    pat = (
        r'<td[^>]*class\s*=\s*"TDListRow ' + re.escape(class_name) + r'(?:[^"]*)"[^>]*>'
        r'(?P<body>.*?)(?=<td\b|</tr>|\Z)'
    )
    m = re.search(pat, html, re.DOTALL | re.IGNORECASE)
    if not m:
        return ""
    body = m.group("body")
    # Take the LAST font block — earlier fonts wrap the label divs.
    fonts = re.findall(
        r'<font[^>]*class="FNTListRow"[^>]*>([^<]+)', body, re.DOTALL,
    )
    if fonts:
        return re.sub(r"\s+", " ", fonts[-1]).strip()
    # Fallback: strip tags entirely and trim.
    text = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", text).strip()


def _extract_billets_numbers(inner: str) -> tuple[str, str]:
    """Pull (Issued, IssuedWL) from a billets sub-table."""
    return _extract_cell(inner, "emis"), _extract_cell(inner, "emis-wl")


def _extract_seats_numbers(inner: str) -> tuple[str, str, str, str]:
    """Pull (Confirmed, Options, WL, Available) from a seats sub-table.

    Available's cell carries an extra `reste` class, sometimes with a
    typo'd `td-dispored reste` instead of `td-dispo reste` — match
    either by anchoring on `reste`.
    """
    confirmed = _extract_cell(inner, "confirm")
    options = _extract_cell(inner, "options")
    wl = _extract_cell(inner, "wl")
    # Available — accept any class containing "reste". End-of-string is
    # a valid cell terminator for the same reason as `_extract_cell`.
    avail_m = re.search(
        r'<td[^>]*class\s*=\s*"[^"]*\breste\b[^"]*"[^>]*>(.*?)(?=<td\b|</tr>|\Z)',
        inner, re.DOTALL,
    )
    available = ""
    if avail_m:
        body = avail_m.group(1)
        # Available text is inside the rightmost <font>; tags often unclosed
        f = re.findall(r'<font[^>]*>([^<]+)', body)
        available = re.sub(r"\s+", " ", f[-1]).strip() if f else ""
    return confirmed, options, wl, available


_INFO_TR_RE = re.compile(
    r'<tr\s+class\s*=\s*"info"[^>]*>(?P<inner>.*?)</tr>',
    re.DOTALL | re.IGNORECASE,
)
_BILLETS_TR_RE = re.compile(
    r'<tr\s+class\s*=\s*"billets"[^>]*>(?P<inner>.*?)</tr>',
    re.DOTALL | re.IGNORECASE,
)
_SIEGES_TR_RE = re.compile(
    r'<tr\s+class\s*=\s*"sieges"[^>]*>(?P<inner>.*?)</tr>',
    re.DOTALL | re.IGNORECASE,
)
# Inventory CELL (vs the wider _INVENTORY_RE that anchors on the parent
# table). Match each cell directly so multi-stop flights with multiple
# inventory cells in the same parent table all surface. The marker
# `inventorystatus` shows up either as an attribute (`inventorystatus="AS"`)
# in real Zenith markup or as a class (`class="inventorystatus"`) in test
# fixtures — match both by anchoring loosely.
_INVENTORY_CELL_RE = re.compile(
    r'inventorystatus[^>]{0,500}?title\s*=\s*(?P<title>[^>]+?)\s*>',
    re.IGNORECASE | re.DOTALL,
)


def parse_flight_loads_html(html: str) -> list[FlightLoadRow]:
    """Parse a Sale_ListeVols_NewStock.asp response into flat leg rows.

    Multi-stop flights emit one row per stop. The legacy markup uses a
    single `data-table-leg` table per flight that contains N
    `<tr class="info">` rows (one per stop), with matching `<tr
    class="billets">` and `<tr class="sieges">` rows in the sibling
    tables. We walk these row-by-row and pair them up by position.
    """
    rows: list[FlightLoadRow] = []
    headers = list(_FLIGHT_HEADER_RE.finditer(html))
    if not headers:
        return rows
    boundaries = [m.start() for m in headers] + [len(html)]

    for i, hdr in enumerate(headers):
        block = html[boundaries[i] : boundaries[i + 1]]
        aircraft_full = hdr.group("aircraft")
        aircraft_type, registration = _split_aircraft(aircraft_full)
        flight_number = f"{hdr.group('airline')}{hdr.group('number')}"

        # Capture each table's inner content (single per flight even for
        # multi-stop), then split into per-stop rows.
        leg_inner = _first_inner(_LEG_TABLE_RE, block)
        billets_inner = _first_inner(_BILLETS_TABLE_RE, block)
        seats_inner = _first_inner(_SEATS_TABLE_RE, block)

        # The `<tr class="info">` rows in the leg table drive the count.
        # Skip the leg-table HEADER row (no <u>route</u>, no time data).
        info_rows = [m.group("inner") for m in _INFO_TR_RE.finditer(leg_inner)]
        if not info_rows:
            # Fallback to the old single-leg shape (no <tr class="info"> wrapper).
            info_rows = [leg_inner]

        billets_rows = [m.group("inner") for m in _BILLETS_TR_RE.finditer(billets_inner)]
        sieges_rows = [m.group("inner") for m in _SIEGES_TR_RE.finditer(seats_inner)]
        # Inventory cells repeat once per stop and live in their own
        # mini-tables. They share the parent's data-id-vol so we can't
        # match by id_vol — walk them in source order and pair by index.
        inv_cells = [
            _strip_inv_title(m.group("title"))
            for m in _INVENTORY_CELL_RE.finditer(block)
        ]

        # The id_vol still comes from the leg-table tag (used as the
        # row's stable key in the cache and across re-runs).
        leg_table_match = _LEG_TABLE_RE.search(block)
        flight_id_vol = leg_table_match.group("id") if leg_table_match else ""

        for stop_idx, leg_row in enumerate(info_rows):
            route_m = _ROUTE_RE.search(leg_row)
            time_m = _LOCAL_TIME_RE.search(leg_row)
            cabin_m = _CABIN_RE.search(leg_row)
            route = f"{route_m.group(1)}-{route_m.group(2)}" if route_m else ""
            origin = route_m.group(1) if route_m else ""
            destination = route_m.group(2) if route_m else ""
            local_time = time_m.group(1).strip() if time_m else ""
            cabin = cabin_m.group(1).strip() if cabin_m else ""

            # Skip header rows that have no extractable data — happens when
            # Zenith puts a label-only row at the top of the leg table.
            if not route and not local_time:
                continue

            billets_row = billets_rows[stop_idx] if stop_idx < len(billets_rows) else ""
            sieges_row = sieges_rows[stop_idx] if stop_idx < len(sieges_rows) else ""
            issued, ticket_wl = _extract_billets_numbers(billets_row)
            confirmed, options, seat_wl, available = _extract_seats_numbers(sieges_row)

            inv_title = inv_cells[stop_idx] if stop_idx < len(inv_cells) else ""

            rows.append(FlightLoadRow(
                flight_number=flight_number,
                day_of_week=hdr.group("dow"),
                flight_date=hdr.group("date"),
                departure_time=hdr.group("time"),
                aircraft=aircraft_type,
                registration=registration,
                total_tickets_issued=hdr.group("tickets"),
                leg_id_vol=flight_id_vol,
                leg_route=route,
                leg_origin=origin,
                leg_destination=destination,
                leg_local_time_range=local_time,
                leg_cabin=cabin,
                tickets_issued=issued,
                tickets_wl=ticket_wl,
                seats_confirmed=confirmed,
                seats_options=options,
                seats_wl=seat_wl,
                seats_available=available,
                inventory_status=inv_title,
            ))
    return rows


def _first_inner(table_re: re.Pattern, block: str) -> str:
    """Return the inner content of the first table matching `table_re`."""
    m = table_re.search(block)
    return m.group("inner") if m else ""


def _strip_inv_title(raw: str) -> str:
    """Inventory cell title can be quoted or bare. Normalise to bare text."""
    return raw.strip().strip('"\'').strip()


def iter_date_chunks(
    date_from: str, date_to: str, chunk_days: int,
) -> Iterable[tuple[str, str]]:
    """Yield (from, to) DD/MM/YYYY pairs covering [date_from, date_to].

    Dates are inclusive on both ends. The chunking keeps each fetch under
    the server's 10-page cap (page_size × 10 records per chunk maximum).
    """
    from datetime import datetime, timedelta
    d_from = datetime.strptime(date_from, "%d/%m/%Y").date()
    d_to = datetime.strptime(date_to, "%d/%m/%Y").date()
    if d_to < d_from:
        return
    cursor = d_from
    while cursor <= d_to:
        end = min(cursor + timedelta(days=chunk_days - 1), d_to)
        yield cursor.strftime("%d/%m/%Y"), end.strftime("%d/%m/%Y")
        cursor = end + timedelta(days=1)


def fetch_flight_loads(
    session: ZenithSession,
    date_from: str,
    date_to: str,
    *,
    page_size: int = FLIGHT_LOAD_DEFAULT_PAGE_SIZE,
    chunk_days: int = 10,
    inter_call_delay_s: float = 1.0,
    progress_cb: Callable[[str, int, int, int], None] | None = None,
    stop_event: threading.Event | None = None,
    timeout_s: float = 60.0,
) -> list[FlightLoadRow]:
    """Pull all flight-load rows in [date_from, date_to] (DD/MM/YYYY).

    Auto-chunks the date range to stay under the server's 10-page cap.
    Within each chunk, paginates until the page returns fewer than
    page_size flight headers (= last page reached) or the cap is hit.

    progress_cb signature: (chunk_label, completed_chunks, total_chunks, rows_so_far)
    """
    chunks = list(iter_date_chunks(date_from, date_to, chunk_days))
    total_chunks = len(chunks)
    all_rows: list[FlightLoadRow] = []

    for chunk_idx, (cfrom, cto) in enumerate(chunks, start=1):
        if stop_event is not None and stop_event.is_set():
            break
        chunk_label = f"{cfrom} → {cto}"
        log.info("Zenith flight-loads chunk %s (page_size=%d)", chunk_label, page_size)

        chunk_rows: list[FlightLoadRow] = []
        for page in range(1, FLIGHT_LOAD_MAX_PAGES + 1):
            if stop_event is not None and stop_event.is_set():
                break
            url = f"{FLIGHT_LOAD_URL}?Nav={page}"
            data = {
                "hidAction": "aff",
                "idVol": "", "ID_Vol": "", "idLeg": "", "idProgVol": "",
                "idvol_toggle": "", "toggleCodeStatusVolGDS": "",
                "BoolAffCriteresGDS": "", "PrgVol_Numero": "",
                "date_depart_vol": cfrom,
                "date_fin_vol": cto,
                "CodeISOAeroDep": "", "CodeISOAeroArr": "",
                "HeureLocale": "HeureLocaleOK",
                "DisplayOppositeLeg": "DisplayOppositeLeg",
                "NbReponse": str(page_size),
                "DayTime": "",
                "VolsOuverts": "VolsOuverts",
                "ID_ETATVOL": "",
            }
            # Retry transient network errors a few times with backoff.
            # Zenith occasionally drops connections mid-request
            # (ConnectionResetError) and read timeouts happen when the
            # server is briefly slow. Without this, one blip kills the
            # whole chunk and the user has to restart from scratch.
            resp = None
            last_exc: Exception | None = None
            for attempt in range(1, 4):  # 3 attempts: 0s, 4s, 10s
                try:
                    resp = session.session.post(
                        url, data=data, timeout=timeout_s, allow_redirects=True,
                    )
                    last_exc = None
                    break
                except requests.RequestException as exc:
                    last_exc = exc
                    if attempt == 3:
                        break
                    backoff = 4 if attempt == 1 else 10
                    log.warning(
                        "Flight-loads %s page %d attempt %d/%d failed (%s) — "
                        "retrying in %ds",
                        chunk_label, page, attempt, 3, exc, backoff,
                    )
                    time.sleep(backoff)
            if resp is None:
                raise ZenithError(
                    f"Network error on chunk {chunk_label} page {page} after "
                    f"3 attempts: {last_exc}"
                ) from last_exc
            if resp.status_code in (401, 403) or "/otds/" in resp.url:
                raise SessionExpiredError(
                    f"Zenith returned {resp.status_code} on flight-loads — "
                    "session expired, please re-login."
                )
            if resp.status_code in (429, 503):
                log.warning("Zenith rate limited on page %d — sleeping 10s", page)
                time.sleep(10.0)
                continue
            resp.raise_for_status()

            page_rows = parse_flight_loads_html(resp.text)
            chunk_rows.extend(page_rows)
            # If fewer flight HEADERS than page_size, we're on the last page.
            # Count distinct flight headers because each flight can have multiple legs.
            distinct_flights = len({(r.flight_number, r.flight_date) for r in page_rows})
            log.info(
                "  page %d: %d rows, %d distinct flights",
                page, len(page_rows), distinct_flights,
            )
            if distinct_flights < page_size:
                break
            if inter_call_delay_s > 0 and page < FLIGHT_LOAD_MAX_PAGES:
                time.sleep(inter_call_delay_s)

        all_rows.extend(chunk_rows)
        if progress_cb:
            try:
                progress_cb(chunk_label, chunk_idx, total_chunks, len(all_rows))
            except Exception:  # noqa: BLE001 — never let callback kill the run
                log.exception("flight-load progress callback raised")
        # Polite gap between chunks
        if inter_call_delay_s > 0 and chunk_idx < total_chunks:
            time.sleep(inter_call_delay_s)
    return all_rows
