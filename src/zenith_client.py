"""HTTP client for Zenith TT Interactive (usba.ttinteractive.com).

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
import random
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

# Zenith serves each carrier from its OWN tenant subdomain — US-Bangla's is
# `usba.ttinteractive.com` (it matches the company code). Login ONLY completes
# on the tenant host: posting credentials to a different host (e.g. the old
# `asia.` regional gateway) just bounces back to /otds/index.asp, which the
# UI shows as "Login rejected by Zenith — landed at …action=TESTLOGIN".
BASE_URL = "https://usba.ttinteractive.com"
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

# Split the flight-load timeout into a (connect, read) tuple. A short connect
# timeout fast-fails a dead network/route; the read timeout stays generous
# because this legacy report is generated slowly server-side (the old flat
# 60s conflated both, so a slow report and a dead network looked identical).
FLIGHT_LOAD_CONNECT_TIMEOUT_S = 10.0
# Capped backoff schedule (seconds) between page retries, indexed by
# (attempt - 1) and clamped to the last entry. Jitter is applied on top so
# retries don't resynchronize (AWS "equal jitter").
_BACKOFF_SCHEDULE_S = (4.0, 10.0)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# Lookup status codes recorded in the cache.
STATUS_OK = "OK"
STATUS_NOT_FOUND = "NOT_FOUND"
STATUS_ERROR = "ERROR"


def _backoff_with_jitter(attempt: int, *, base_s: float, cap_s: float) -> float:
    """Capped exponential backoff (seconds) with equal jitter for retry
    `attempt` (1-based): keep half the wait, randomize the other half so
    concurrent workers that fail together don't all retry in lockstep."""
    raw = min(cap_s, base_s * (2 ** (max(1, attempt) - 1)))
    return raw / 2.0 + random.uniform(0.0, raw / 2.0)


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
        self, customer_id: str, *, timeout_s: float = 45.0, max_attempts: int = 4,
    ) -> CustomerRecord:
        """Fetch and parse one customer record, retrying transient failures.

        Zenith intermittently returns 502/503/504 (gateway timeout / overload)
        on bulk runs; those — and network blips — are retried with capped
        backoff + jitter instead of failing the ID on the first hiccup.
        Session loss (401/403 or a login redirect) is NOT retried; it needs a
        fresh login. Raises CustomerNotFoundError, SessionExpiredError,
        RateLimitedError, or ZenithError once retries are exhausted.
        """
        params = {"IdCustomer": str(customer_id).strip()}
        attempts = max(1, int(max_attempts))
        for attempt in range(1, attempts + 1):
            is_last = attempt >= attempts
            try:
                resp = self.session.get(
                    CUSTOMER_LOOKUP_URL,
                    params=params,
                    allow_redirects=True,
                    timeout=timeout_s,
                )
            except requests.RequestException as exc:
                if is_last:
                    raise ZenithError(
                        f"Network error fetching {customer_id} after "
                        f"{attempts} attempts: {exc}"
                    ) from exc
                time.sleep(_backoff_with_jitter(attempt, base_s=1.5, cap_s=8.0))
                continue

            # Session loss — never retry; the caller must re-login.
            if resp.status_code in (401, 403) or "/otds/" in resp.url:
                raise SessionExpiredError(
                    f"Zenith returned {resp.status_code} for ID {customer_id} — "
                    "session expired or never authenticated."
                )
            # Rate limited / temporarily unavailable — back off (longer) + retry.
            if resp.status_code in (429, 503):
                if is_last:
                    raise RateLimitedError(
                        f"Zenith returned {resp.status_code} for ID "
                        f"{customer_id} after {attempts} attempts — back off."
                    )
                time.sleep(_backoff_with_jitter(attempt, base_s=4.0, cap_s=12.0))
                continue
            # Transient gateway/server errors — the 502/503/504 (and 500) seen
            # on bulk runs. Retry; only fail the ID if it sticks.
            if resp.status_code >= 500:
                if is_last:
                    raise ZenithError(
                        f"Zenith returned {resp.status_code} for ID "
                        f"{customer_id} after {attempts} attempts."
                    )
                time.sleep(_backoff_with_jitter(attempt, base_s=1.5, cap_s=8.0))
                continue
            # 2xx/3xx (not a login redirect) — parse it. A page with NO customer form
            # at all is almost always a degraded/error response under overload: a genuinely
            # missing ID returns an EMPTY FORM (markers present, fields blank), so "no markers"
            # means we didn't get the form. Retry it like any transient; only the final
            # attempt lets CustomerNotFoundError through (so a real miss is still reported).
            try:
                return parse_customer_html(resp.text, str(customer_id))
            except CustomerNotFoundError:
                if is_last:
                    raise
                time.sleep(_backoff_with_jitter(attempt, base_s=1.5, cap_s=8.0))
                continue
        # The loop always returns or raises on the final attempt; guard anyway.
        raise ZenithError(f"Exhausted retries fetching {customer_id}.")


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
    retry_passes: int = 2,
    retry_cooldown_s: float = 30.0,
    on_notice: Callable[[str], None] | None = None,
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

    # --- Automatic retry sweeps for transient (504/network) failures ----------
    # Zenith's origin 504-storms intermittently; a record that timed out now often
    # succeeds a minute later. So instead of dropping it, we cool down (let the origin
    # recover) and re-sweep ONLY the transient failures, up to `retry_passes` times.
    # Re-firing progress_cb lets the caller's cache flip ERROR->OK, so the final output
    # (written from that cache) self-corrects. Not-found / session-loss / cancelled are
    # NOT retried. The cooldown escalates (30s, 60s, ...) to give a longer storm time.
    def _notice(msg: str) -> None:
        log.info(msg)
        if on_notice is not None:
            try:
                on_notice(msg)
            except Exception:  # noqa: BLE001 — never let a callback kill the run
                log.exception("on_notice raised")

    def _is_retryable(r: LookupResult) -> bool:
        if r.status != STATUS_ERROR:
            return False
        err = r.error or ""
        return "cancelled" not in err and "SessionExpired" not in err

    by_id: dict[str, LookupResult] = {}
    for r in results:
        by_id[r.customer_id] = r          # keep the latest outcome per id

    for sweep in range(1, max(0, int(retry_passes)) + 1):
        if stop_event is not None and stop_event.is_set():
            break
        retry_ids = [cid for cid, r in by_id.items() if _is_retryable(r)]
        if not retry_ids:
            break
        cooldown = retry_cooldown_s * sweep
        _notice(f"{len(retry_ids)} transient failure(s) — cooling down {cooldown:.0f}s, "
                f"then retry sweep {sweep}/{retry_passes}…")
        waited = 0.0
        while waited < cooldown:
            if stop_event is not None and stop_event.is_set():
                break
            time.sleep(0.5)
            waited += 0.5
        if stop_event is not None and stop_event.is_set():
            break
        _notice(f"Retry sweep {sweep}/{retry_passes}: re-attempting {len(retry_ids)} ID(s)…")
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(_worker, cid): cid for cid in retry_ids}
            for fut in as_completed(futures):
                r = fut.result()
                by_id[r.customer_id] = r
                results.append(r)
                if progress_cb is not None:
                    try:
                        progress_cb(r, total, total)   # bar stays full; row self-corrects
                    except Exception:  # noqa: BLE001
                        log.exception("progress callback raised")
                if stop_event is not None and stop_event.is_set():
                    for remaining in futures:
                        if not remaining.done():
                            remaining.cancel()
                    break

    # Retry sweeps append a corrected result per recovered id, so `results` can hold an id
    # more than once. The GUI writes from its cache (so it's unaffected), but a non-GUI
    # caller iterating the return would double-process — so collapse to the LATEST per id.
    return list({r.customer_id: r for r in results}.values())


# ---------------------------------------------------------------------------
# Leg classification — Domestic/International + Inbound/Outbound
# ---------------------------------------------------------------------------

# US-Bangla's domestic network. Everything else is treated as international.
BD_AIRPORTS = frozenset({
    "DAC",  # Dhaka (hub)
    "CGP",  # Chattogram
    "ZYL",  # Sylhet
    "CXB",  # Cox's Bazar
    "JSR",  # Jessore
    "SPD",  # Saidpur
    "RJH",  # Rajshahi
    "BZL",  # Barishal
})
HUB_AIRPORT = "DAC"


def classify_leg_region(origin: str, destination: str) -> str:
    """'Domestic' when both endpoints are BD airports, else 'International'."""
    o, d = (origin or "").upper(), (destination or "").upper()
    return "Domestic" if (o in BD_AIRPORTS and d in BD_AIRPORTS) else "International"


def classify_leg_direction(origin: str, destination: str) -> str:
    """'Outbound' (leaving hub/country) or 'Inbound' (returning).

    International legs key off the BD border; domestic legs key off the
    DAC hub. Falls back to Outbound for the rare non-hub domestic leg.
    """
    o, d = (origin or "").upper(), (destination or "").upper()
    o_bd, d_bd = o in BD_AIRPORTS, d in BD_AIRPORTS
    if o_bd and not d_bd:
        return "Outbound"          # leaving Bangladesh
    if d_bd and not o_bd:
        return "Inbound"           # entering Bangladesh
    if o == HUB_AIRPORT:
        return "Outbound"          # domestic, departing hub
    if d == HUB_AIRPORT:
        return "Inbound"           # domestic, arriving hub
    return "Outbound"


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

    # Passenger-manifest drill-down keys, parsed from the leg's
    # liste_passager_vol.asp link. Empty when the link isn't present.
    leg_id_leg: str = ""      # id_leg for the passenger-list page
    leg_id_aero: str = ""     # id_aero (departure airport id)


# ---- regexes — anchored on stable structural markers ----

# Per-leg passenger-list link inside each leg info-row. `&amp;` because
# the href is HTML-escaped in the page source.
_PAX_LINK_RE = re.compile(
    r"liste_passager_vol\.asp\?id_vol=(?P<vol>\d+)&(?:amp;)?id_leg=(?P<leg>\d+)"
    r"&(?:amp;)?id_aero=(?P<aero>\d+)",
    re.IGNORECASE,
)

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

            # Passenger-manifest drill-down keys come from this leg's
            # liste_passager_vol.asp link. The link's id_vol is per-stop
            # (more precise than the parent leg-table id for multi-stop
            # flights), so prefer it when present.
            link_m = _PAX_LINK_RE.search(leg_row)
            if link_m:
                row_id_vol = link_m.group("vol")
                row_id_leg = link_m.group("leg")
                row_id_aero = link_m.group("aero")
            else:
                row_id_vol, row_id_leg, row_id_aero = flight_id_vol, "", ""

            rows.append(FlightLoadRow(
                flight_number=flight_number,
                day_of_week=hdr.group("dow"),
                flight_date=hdr.group("date"),
                departure_time=hdr.group("time"),
                aircraft=aircraft_type,
                registration=registration,
                total_tickets_issued=hdr.group("tickets"),
                leg_id_vol=row_id_vol or flight_id_vol,
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
                leg_id_leg=row_id_leg,
                leg_id_aero=row_id_aero,
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


def _flight_row_key(r: "FlightLoadRow") -> tuple[str, str, str, str]:
    """Stable identity for a single (flight, date, leg, cabin) load row.

    Multi-stop flights produce several rows that share flight_number +
    flight_date but differ by leg_route, so the leg + cabin must be part
    of the key. Used to dedup across paginated responses.
    """
    return (r.flight_number, r.flight_date, r.leg_route, r.leg_cabin)


class _PageServerError(ZenithError):
    """A page persistently returned HTTP 5xx (e.g. 500 on a deep Nav page).

    The server can't service this page for this (possibly too-large) date
    range. Internal signal — the range fetcher catches it and SPLITS the
    chunk instead of aborting the run, since a smaller chunk needs fewer
    pages and won't reach the 500-prone deep pages.
    """


def _post_flight_load_page(
    session: ZenithSession,
    cfrom: str,
    cto: str,
    page: int,
    page_size: int,
    *,
    timeout_s: float,
    chunk_label: str,
):
    """POST one Nav={page} request with retry/backoff. Returns the response.

    Uses a (connect, read) timeout tuple so a dead network fast-fails while
    the slow report still gets a generous read window. Retries transient
    failures with capped backoff + jitter. On:
      - session loss (401/403/login redirect) → SessionExpiredError
      - rate limit (429/503) → returns None (caller retries the same page)
      - persistent 5xx OR read timeout after 3 tries → _PageServerError
        (caller splits the range — both mean "too heavy to serve in time")
      - dead network / connect failure after 3 tries → ZenithError
        (aborts; environmental — a smaller range won't help)
    """
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
    last_exc: Exception | None = None
    for attempt in range(1, 4):  # 3 attempts; capped backoff + jitter between
        # Capped backoff with equal jitter (AWS Builders' Library): keep half
        # the scheduled wait, randomize the other half, so retries that bunch
        # up don't all fire again at the same instant.
        base = _BACKOFF_SCHEDULE_S[min(attempt - 1, len(_BACKOFF_SCHEDULE_S) - 1)]
        backoff = base / 2.0 + random.uniform(0.0, base / 2.0)
        try:
            resp = session.session.post(
                url, data=data,
                timeout=(FLIGHT_LOAD_CONNECT_TIMEOUT_S, timeout_s),
                allow_redirects=True,
            )
        except requests.exceptions.ReadTimeout as exc:
            # The server accepted the request but didn't answer within the
            # read window. For this read-only report POST that's the SAME
            # signal as an HTTP 5xx: the date range is too heavy to serve in
            # time. So after 3 tries raise a SPLITTABLE error — a smaller
            # range needs less server work and comes back in time — rather
            # than aborting the run. Re-issuing the identical search is safe
            # because the POST has no side effects (it only runs a query).
            last_exc = exc
            if attempt == 3:
                raise _PageServerError(
                    f"Read timeout on chunk {chunk_label} page {page} after "
                    f"3 attempts: {exc}"
                ) from exc
            log.warning(
                "Flight-loads %s page %d attempt %d/3 read timeout (%s) — "
                "will split if it persists; retrying in %.1fs",
                chunk_label, page, attempt, exc, backoff,
            )
            time.sleep(backoff)
            continue
        except requests.RequestException as exc:
            # Connect timeout, DNS failure, connection reset — the request
            # most likely never reached the server, and a smaller range
            # won't help. Environmental, so abort after 3 tries.
            last_exc = exc
            if attempt == 3:
                raise ZenithError(
                    f"Network error on chunk {chunk_label} page {page} after "
                    f"3 attempts: {exc}"
                ) from exc
            log.warning(
                "Flight-loads %s page %d attempt %d/3 network error (%s) — "
                "retrying in %.1fs", chunk_label, page, attempt, exc, backoff,
            )
            time.sleep(backoff)
            continue

        # Got a response — classify by status.
        if resp.status_code in (401, 403) or "/otds/" in resp.url:
            raise SessionExpiredError(
                f"Zenith returned {resp.status_code} on flight-loads — "
                "session expired, please re-login."
            )
        if resp.status_code in (429, 503):
            log.warning("Zenith rate limited on page %d — sleeping 10s", page)
            time.sleep(10.0)
            return None  # caller retries the same page
        if resp.status_code in (500, 502, 504):
            # Zenith 500s on deep Nav pages of big chunks. Retry a couple
            # of times (could be transient load); if it sticks, signal a
            # split rather than killing the whole run.
            if attempt == 3:
                raise _PageServerError(
                    f"Zenith {resp.status_code} on chunk {chunk_label} "
                    f"page {page} after 3 attempts"
                )
            log.warning(
                "Zenith %d on chunk %s page %d attempt %d/3 — retrying in %.1fs",
                resp.status_code, chunk_label, page, attempt, backoff,
            )
            time.sleep(backoff)
            continue
        resp.raise_for_status()  # any other 4xx is a real client error
        return resp
    # Unreachable: every path in the loop returns/raises/continues.
    raise ZenithError(f"Flight-loads page {page} exhausted retries: {last_exc}")


def _fetch_flight_load_range(
    session: ZenithSession,
    cfrom: str,
    cto: str,
    *,
    page_size: int,
    inter_call_delay_s: float,
    stop_event: threading.Event | None,
    timeout_s: float,
) -> tuple[list["FlightLoadRow"], bool]:
    """Fetch every page for one date range, deduping across pages.

    Returns (rows, needs_split). `needs_split` is True when either we hit
    the FLIGHT_LOAD_MAX_PAGES ceiling with data still arriving, OR the
    server returned a persistent 5xx on a deep page — both mean the range
    is too big; the caller should split it rather than truncate or abort.

    The stop condition is exhaustion-based, never the old
    `distinct_flights < page_size` heuristic (which mistook a full page
    of multi-stop flights for the last page and dropped whole days).
    """
    chunk_label = f"{cfrom} → {cto}"
    log.info("Zenith flight-loads range %s (page_size=%d)", chunk_label, page_size)
    range_rows: list[FlightLoadRow] = []
    range_seen: set[tuple[str, str, str, str]] = set()
    hit_cap_with_data = False
    rate_limit_retries = 0

    page = 1
    while page <= FLIGHT_LOAD_MAX_PAGES:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            resp = _post_flight_load_page(
                session, cfrom, cto, page, page_size,
                timeout_s=timeout_s, chunk_label=chunk_label,
            )
        except _PageServerError as exc:
            # Server can't serve this page for this range size. Signal a
            # split — smaller halves need fewer pages and avoid the 500.
            log.warning(
                "Flight-loads %s page %d server-errored (%s) — splitting chunk",
                chunk_label, page, exc,
            )
            hit_cap_with_data = True
            break
        if resp is None:
            # Rate limited — _post already slept 10s. Retry the SAME page,
            # but bound it so a server that throttles forever can't hang
            # the whole run. After 5 throttles on one page, give up loudly.
            rate_limit_retries += 1
            if rate_limit_retries > 5:
                raise RateLimitedError(
                    f"Zenith kept rate-limiting chunk {chunk_label} page "
                    f"{page} (6 attempts). Try again later or raise the delay."
                )
            continue
        rate_limit_retries = 0  # reset on any successful page fetch

        page_rows = parse_flight_loads_html(resp.text)
        if not page_rows:
            # Genuine exhaustion: server has no more rows for this range.
            log.info("  page %d: 0 rows — range complete", page)
            break

        new_rows = []
        for r in page_rows:
            key = _flight_row_key(r)
            if key not in range_seen:
                range_seen.add(key)
                new_rows.append(r)
        range_rows.extend(new_rows)
        log.info(
            "  page %d: %d rows (%d new, %d cumulative)",
            page, len(page_rows), len(new_rows), len(range_rows),
        )

        if not new_rows:
            # Every row on this page was already seen — exhausted. This
            # also safely handles a server that ignores Nav and re-serves
            # page 1 (broken pagination → second page is all dupes → stop).
            break

        if page == FLIGHT_LOAD_MAX_PAGES:
            # We stopped because of the cap, not exhaustion: more rows
            # likely exist beyond page 10. Signal the caller to split.
            hit_cap_with_data = True

        page += 1
        if inter_call_delay_s > 0 and page <= FLIGHT_LOAD_MAX_PAGES:
            time.sleep(inter_call_delay_s)

    return range_rows, hit_cap_with_data


def _split_date_range(cfrom: str, cto: str) -> tuple[tuple[str, str], tuple[str, str]]:
    """Split [cfrom, cto] (DD/MM/YYYY, inclusive) into two halves by date."""
    from datetime import datetime, timedelta
    d_from = datetime.strptime(cfrom, "%d/%m/%Y").date()
    d_to = datetime.strptime(cto, "%d/%m/%Y").date()
    span = (d_to - d_from).days
    mid = d_from + timedelta(days=span // 2)
    fmt = "%d/%m/%Y"
    return (
        (d_from.strftime(fmt), mid.strftime(fmt)),
        ((mid + timedelta(days=1)).strftime(fmt), d_to.strftime(fmt)),
    )


def _log_flight_load_completeness(
    rows: list["FlightLoadRow"], date_from: str, date_to: str,
) -> list[str]:
    """Log per-date row counts and flag any date with ZERO rows.

    Returns the list of missing dates so callers can surface them. The
    whole point: a gap can NEVER be silent again — it screams in the log.
    """
    from collections import Counter
    from datetime import datetime, timedelta
    by_date: Counter[str] = Counter(r.flight_date for r in rows)
    d_from = datetime.strptime(date_from, "%d/%m/%Y").date()
    d_to = datetime.strptime(date_to, "%d/%m/%Y").date()
    missing: list[str] = []
    cursor = d_from
    while cursor <= d_to:
        key = cursor.strftime("%d/%m/%Y")
        if by_date.get(key, 0) == 0:
            missing.append(key)
        cursor += timedelta(days=1)
    log.info(
        "Flight-loads completeness: %d rows across %d dates (%s..%s)",
        len(rows), len(by_date), date_from, date_to,
    )
    if missing:
        log.warning(
            "Flight-loads MISSING %d date(s) with zero rows: %s",
            len(missing), ", ".join(missing),
        )
    return missing


def fetch_flight_loads(
    session: ZenithSession,
    date_from: str,
    date_to: str,
    *,
    page_size: int = FLIGHT_LOAD_DEFAULT_PAGE_SIZE,
    chunk_days: int = 5,
    inter_call_delay_s: float = 1.0,
    progress_cb: Callable[[str, int, int, int], None] | None = None,
    stop_event: threading.Event | None = None,
    timeout_s: float = 120.0,
) -> list[FlightLoadRow]:
    """Pull all flight-load rows in [date_from, date_to] (DD/MM/YYYY).

    Robust against the server's 10-page-per-search cap: the date range is
    chunked, each chunk paginated with cross-page dedup, and **any chunk
    that hits the page cap with data still arriving is automatically
    split in half and re-fetched** — so flight density or a large range
    can never silently truncate a chunk. `chunk_days` is now only a
    performance hint (initial granularity); correctness no longer depends
    on it.

    progress_cb signature: (chunk_label, completed_chunks, total_chunks, rows_so_far)
    """
    from collections import deque

    work: deque[tuple[str, str]] = deque(
        iter_date_chunks(date_from, date_to, chunk_days)
    )
    total_known = len(work)        # grows as oversized chunks split
    completed = 0
    all_rows: list[FlightLoadRow] = []
    global_seen: set[tuple[str, str, str, str]] = set()

    while work:
        if stop_event is not None and stop_event.is_set():
            break
        cfrom, cto = work.popleft()
        range_rows, capped = _fetch_flight_load_range(
            session, cfrom, cto,
            page_size=page_size,
            inter_call_delay_s=inter_call_delay_s,
            stop_event=stop_event,
            timeout_s=timeout_s,
        )

        same_day = cfrom == cto
        if capped and not same_day:
            # Range too big for the 10-page cap — split and re-fetch the
            # halves fresh (discard the partial rows to avoid a torn
            # boundary). This is what makes completeness guaranteed.
            (lo, hi) = _split_date_range(cfrom, cto)
            log.warning(
                "Flight-loads chunk %s → %s hit the %d-page cap with data "
                "still arriving — splitting into %s→%s and %s→%s",
                cfrom, cto, FLIGHT_LOAD_MAX_PAGES,
                lo[0], lo[1], hi[0], hi[1],
            )
            work.appendleft(hi)
            work.appendleft(lo)
            total_known += 1  # one range became two
            continue
        if capped and same_day:
            # A single day exceeding 1000 distinct flights is impossible
            # for this carrier — but never fail silently if it happens.
            log.error(
                "Flight-loads single day %s exceeded the %d-page cap; "
                "data for that day may be incomplete.",
                cfrom, FLIGHT_LOAD_MAX_PAGES,
            )

        for r in range_rows:
            key = _flight_row_key(r)
            if key not in global_seen:
                global_seen.add(key)
                all_rows.append(r)

        completed += 1
        if progress_cb:
            try:
                progress_cb(f"{cfrom} → {cto}", completed, total_known, len(all_rows))
            except Exception:  # noqa: BLE001 — never let callback kill the run
                log.exception("flight-load progress callback raised")
        # Polite gap between ranges.
        if inter_call_delay_s > 0 and work:
            time.sleep(inter_call_delay_s)

    _log_flight_load_completeness(all_rows, date_from, date_to)
    return all_rows


# ---------------------------------------------------------------------------
# Passenger manifest (per-leg drill-down) — liste_passager_vol.asp
# ---------------------------------------------------------------------------

PAX_MANIFEST_URL = (
    f"{BASE_URL}/newui/aerien/gestionregulation/liste_passager_vol.asp"
)


@dataclass(frozen=True)
class PassengerRecord:
    """One passenger row from a flight-leg's passenger list (PNL detail).

    Carries the full manifest — including sensitive PII (passport, DOB) —
    because the user explicitly asked for every field. Treat exported
    files as confidential.
    """

    # Flight context (from the page header + drill-down keys)
    flight_number: str
    flight_date: str          # DD/MM/YYYY
    flight_time: str          # HH:MM
    route_desc: str           # "Dubai Intl - Dhaka"
    id_vol: str
    id_leg: str

    # Passenger identity
    title: str                # Mr./Mrs./Ms./Mstr
    full_name: str            # "ABBAS MOHAMMOD"
    pax_type: str             # AD / CH / INF
    gender: str               # M / F
    date_of_birth: str        # DD/MM/YYYY (PII)
    passport_no: str          # (PII)
    weight_kg: str

    # Booking / fare
    cabin_code: str           # Y
    prbd: str                 # E  (booking class letter)
    fare_basis: str           # EDXBO (Cl.)
    web_class: str            # Economy Lite
    ticket_number: str        # 7792...
    seat: str                 # 19A
    pnr: str                  # host PNR
    gds_pnr: str              # GDS PNR (2nd code if present)
    leg: str                  # "DXB DAC"
    issuing_agency: str       # "TA: 86219313 ..."
    direction: str            # Outbound / Inbound


_PAX_HEADER_RE = re.compile(
    r"Flight\s*:\s*(?P<flt>BS\s*\d+)\s+(?P<route>.+?)\s+The\s+"
    r"(?P<date>\d{2}/\d{2}/\d{4})\s+at\s+(?P<time>\d{2}:\d{2})",
)
_PAX_ROW_SPLIT_RE = re.compile(r'<td class="Col_NomPrenomAge"', re.IGNORECASE)
_PAX_CELL_RE_TMPL = r'<td class="{col}"[^>]*>(?P<body>.*?)</td>'

# Field sub-patterns within the name/age cell.
_PAX_TITLE_RE = re.compile(r"\b(Mr\.|Mrs\.|Ms\.|Mstr\.?|Miss|Dr\.)", re.IGNORECASE)
_PAX_TYPE_GENDER_RE = re.compile(r"\b(AD|CHD?|INF)\s*-\s*([MF])\b")
_PAX_DOB_RE = re.compile(r"Born on:\s*(\d{2}/\d{2}/\d{4})")
_PAX_PASSPORT_RE = re.compile(r"Passport\s*No\.?\s*:\s*([A-Za-z0-9]+)")
_PAX_WEIGHT_RE = re.compile(r"(\d+)\s*Kg", re.IGNORECASE)
_TICKET_NUM_RE = re.compile(r"\b(\d{13})\b")


def _clean_cell(html_fragment: str) -> str:
    """Strip tags + collapse whitespace + decode the few entities we see.

    The legacy markup sometimes emits a malformed `&nbsp` with no trailing
    semicolon (e.g. `Born on:&nbsp25/01/1988`), so we match the optional
    semicolon — otherwise the DOB glues to the label and won't parse.
    """
    text = re.sub(r"<[^>]+>", " ", html_fragment)
    text = re.sub(r"&nbsp;?", " ", text)
    text = text.replace("&amp;", "&").replace("&#45;", "-")
    return re.sub(r"\s+", " ", text).strip()


def _pax_cell(row_html: str, col: str, label: str) -> str:
    """Extract one Col_* cell value, stripping its repeated xs-left label."""
    m = re.search(_PAX_CELL_RE_TMPL.format(col=col), row_html, re.DOTALL | re.IGNORECASE)
    if not m:
        return ""
    val = _clean_cell(m.group("body"))
    # Each cell repeats the column header as an xs-left label; drop it.
    if label and val.lower().startswith(label.lower()):
        val = val[len(label):].strip()
    return val


def parse_passenger_list_html(
    html: str, *, id_vol: str = "", id_leg: str = "",
) -> list[PassengerRecord]:
    """Parse a liste_passager_vol.asp page into PassengerRecord rows."""
    hdr = _PAX_HEADER_RE.search(re.sub(r"<[^>]+>", " ", html))
    if hdr:
        flight_number = re.sub(r"\s+", "", hdr.group("flt"))
        route_desc = re.sub(r"\s+", " ", hdr.group("route")).strip()
        flight_date = hdr.group("date")
        flight_time = hdr.group("time")
    else:
        flight_number = route_desc = flight_date = flight_time = ""

    records: list[PassengerRecord] = []
    # Each passenger row contains exactly one Col_NomPrenomAge cell.
    marks = [m.start() for m in _PAX_ROW_SPLIT_RE.finditer(html)]
    for pos in marks:
        rstart = html.rfind("<tr", 0, pos)
        rend = html.find("</tr>", pos)
        if rstart < 0 or rend < 0:
            continue
        row = html[rstart:rend]

        name_blob = _pax_cell(row, "Col_NomPrenomAge", "Surname, First name, Weight")
        # The blob may begin with a leftover material-icon glyph token; the
        # title regex anchors us regardless.
        title = ""
        tm = _PAX_TITLE_RE.search(name_blob)
        if tm:
            title = tm.group(1)
        tg = _PAX_TYPE_GENDER_RE.search(name_blob)
        pax_type = (tg.group(1) if tg else "")
        gender = (tg.group(2) if tg else "")
        dob = (_PAX_DOB_RE.search(name_blob).group(1)
               if _PAX_DOB_RE.search(name_blob) else "")
        passport = (_PAX_PASSPORT_RE.search(name_blob).group(1)
                    if _PAX_PASSPORT_RE.search(name_blob) else "")
        weight = (_PAX_WEIGHT_RE.search(name_blob).group(1)
                  if _PAX_WEIGHT_RE.search(name_blob) else "")
        # Name = text between the title and the pax-type token.
        full_name = name_blob
        if tm:
            full_name = name_blob[tm.end():]
        if tg:
            cut = full_name.find(tg.group(0))
            if cut >= 0:
                full_name = full_name[:cut]
        full_name = re.sub(r"\s+", " ", full_name).strip(" -·")

        pnr_raw = _pax_cell(row, "Col_PNR", "PNR")
        pnr_parts = pnr_raw.split()
        pnr = pnr_parts[0] if pnr_parts else ""
        gds_pnr = pnr_parts[1] if len(pnr_parts) > 1 else ""

        billet_raw = _pax_cell(row, "Col_Billet", "Ticket")
        tk = _TICKET_NUM_RE.search(billet_raw)
        ticket_number = tk.group(1) if tk else ""

        records.append(PassengerRecord(
            flight_number=flight_number,
            flight_date=flight_date,
            flight_time=flight_time,
            route_desc=route_desc,
            id_vol=id_vol,
            id_leg=id_leg,
            title=title,
            full_name=full_name,
            pax_type=pax_type,
            gender=gender,
            date_of_birth=dob,
            passport_no=passport,
            weight_kg=weight,
            cabin_code=_pax_cell(row, "Col_CabineClasseCode", "Cabin code"),
            prbd=_pax_cell(row, "Col_PRBD", "PRBD"),
            fare_basis=_pax_cell(row, "Col_Classe", "Cl."),
            web_class=_pax_cell(row, "Col_Webclasse", "Web Cl."),
            ticket_number=ticket_number,
            seat=_pax_cell(row, "Col_SEAT", ""),
            pnr=pnr,
            gds_pnr=gds_pnr,
            leg=_pax_cell(row, "Col_Leg", "Leg"),
            issuing_agency=_pax_cell(row, "Col_AgenceEmettrice", "Issuing agency"),
            direction=_pax_cell(row, "Col_AR", "OUT/IN"),
        ))
    return records


def fetch_passenger_manifest(
    session: ZenithSession,
    id_vol: str,
    id_leg: str,
    id_aero: str,
    *,
    timeout_s: float = 120.0,
) -> list[PassengerRecord]:
    """Fetch + parse one flight-leg's passenger list.

    Replicates the print-mode GET the Zenith UI issues, which returns the
    entire manifest in a single response (no pagination).
    """
    params = {
        "id_vol": str(id_vol), "id_leg": str(id_leg), "id_aero": str(id_aero),
        "m": "0", "step": "1", "ID_LegManifeste": str(id_leg),
        "TypePassager_Lib": "Passenger type", "ID_TypePassager": "",
        "OptionAffichage": "5", "ModePrint": "ok", "AutoPrint": "off",
        "IATASSRCODE": "", "DisplayCustomerInfo": "false",
        "InvalidConnectingFlight": "false", "PRBDCode": "",
        "IssuingAgency": "", "ID_TypeIssuingAgency": "",
    }
    sess = session.session
    sess.headers.setdefault("User-Agent", USER_AGENT)
    try:
        resp = sess.get(PAX_MANIFEST_URL, params=params, timeout=timeout_s)
    except requests.RequestException as exc:
        raise ZenithError(
            f"Network error fetching manifest id_vol={id_vol} id_leg={id_leg}: {exc}"
        ) from exc
    if resp.status_code in (401, 403) or "/otds/" in resp.url:
        raise SessionExpiredError(
            f"Zenith returned {resp.status_code} on the passenger manifest — "
            "session expired, please re-login."
        )
    resp.raise_for_status()
    return parse_passenger_list_html(resp.text, id_vol=str(id_vol), id_leg=str(id_leg))
